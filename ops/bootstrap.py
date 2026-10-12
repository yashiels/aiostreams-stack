#!/usr/bin/env python3
import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import urllib.parse
from pathlib import Path

from common import (
    Plan,
    QuietHttp,
    RequestError,
    Settings,
    aiostreams_session,
    basic,
    canonical_config,
    reconciliation_get,
    resolve_target,
    save_snapshot,
    snapshot_path,
)
from mdblist import configure_rows

ROOT = Path(__file__).resolve().parent.parent
PERSONA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MANAGED_PERSONA_FIELDS = ("id", "name", "avatar", "history")


def avatar_url(user):
    query = urllib.parse.urlencode(
        {
            "name": user["name"].title(),
            "size": 512,
            "background": user["color"],
            "color": "ffffff",
            "bold": "true",
            "font-size": 0.42,
            "format": "png",
        }
    )
    return f"https://ui-avatars.com/api/?{query}"


def persona_id(slug):
    value = re.sub(r"[^a-z0-9_-]", "", slug.lower())
    if not PERSONA_ID.fullmatch(value):
        raise SystemExit(f"invalid persona id derived from household slug {slug!r}")
    return value


def desired_personas(household, current_personas=()):
    existing_by_id = {persona.get("id"): persona for persona in current_personas}
    personas = []
    for user in household["users"]:
        if user.get("admin"):
            continue
        identifier = persona_id(user["slug"])
        existing = existing_by_id.get(identifier, {})
        preserved = {
            key: value
            for key, value in existing.items()
            if key not in MANAGED_PERSONA_FIELDS
        }
        personas.append(
            {
                **preserved,
                "id": identifier,
                "name": user["name"],
                "avatar": avatar_url(user),
                "history": "own",
            }
        )
    return personas


def desired_jellyfin(household, current):
    admins = [user for user in household["users"] if user.get("admin")]
    if len(admins) != 1:
        raise SystemExit("household config must contain exactly one admin")
    primary = dict(current.get("primary") or {})
    primary.update(name=admins[0]["name"], avatar=avatar_url(admins[0]))
    desired = dict(current)
    desired.update(
        resolveOnOpen=True,
        maxVersions=10,
        segments=True,
        primary=primary,
        personas=desired_personas(household, current.get("personas") or []),
    )
    return desired


def managed_persona(persona):
    return {key: persona.get(key) for key in MANAGED_PERSONA_FIELDS}


def managed_jellyfin(jellyfin):
    primary = jellyfin.get("primary") or {}
    return {
        "resolveOnOpen": jellyfin.get("resolveOnOpen"),
        "maxVersions": jellyfin.get("maxVersions"),
        "segments": jellyfin.get("segments"),
        "primary": {"name": primary.get("name"), "avatar": primary.get("avatar")},
        "personas": [
            managed_persona(persona) for persona in jellyfin.get("personas") or []
        ],
    }


def configured_template(torbox_key=None, settings=None):
    settings = settings or Settings(ROOT)
    template = configure_rows(
        json.loads((ROOT / "config" / "aiostreams.template.json").read_text()),
        settings.get("MDBLIST_API_KEY"),
        settings.get("MDBLIST_ROWS"),
    )
    if torbox_key is None:
        return template
    services = template.get("services") or []
    torbox = next(
        (service for service in services if service.get("id") == "torbox"), None
    )
    if torbox is None:
        raise SystemExit("template has no torbox service")
    torbox["credentials"] = {"apiKey": torbox_key}
    return template


def apply_template(current, template):
    desired = copy.deepcopy(current)
    for key, value in template.items():
        if key != "services":
            desired[key] = copy.deepcopy(value)
    current_services = {
        service.get("id"): service for service in current.get("services") or []
    }
    desired_services = []
    for template_service in template.get("services") or []:
        service = copy.deepcopy(template_service)
        existing = current_services.get(service.get("id"), {})
        if "credentials" in existing:
            service["credentials"] = copy.deepcopy(existing["credentials"])
        elif service.get("id") in current_services:
            service.pop("credentials", None)
        desired_services.append(service)
    template_ids = {service.get("id") for service in desired_services}
    desired_services.extend(
        copy.deepcopy(service)
        for service in current.get("services") or []
        if service.get("id") not in template_ids
    )
    desired["services"] = desired_services
    return desired


def without_credentials(value):
    if isinstance(value, dict):
        return {
            key: without_credentials(item)
            for key, item in value.items()
            if key != "credentials"
        }
    if isinstance(value, list):
        return [without_credentials(item) for item in value]
    return value


def hash_state(config, keys):
    state = {key: copy.deepcopy(config[key]) for key in keys if key in config}
    for key in ("services", "presets"):
        for item in state.get(key) or []:
            item.pop("credentials", None)
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sync_sha256(config, template):
    return hash_state(config, template)


def sync_template(current, template, allow_preset_removal=False):
    before = unique_presets(current.get("presets") or [])
    after = unique_presets(template.get("presets") or [])
    if before.keys() - after.keys() and not allow_preset_removal:
        raise SystemExit("preset removal requires --allow-preset-removal")
    desired = apply_template(current, template)
    for preset in desired.get("presets") or []:
        existing = before.get(preset["instanceId"], {})
        if "credentials" in existing:
            preset["credentials"] = copy.deepcopy(existing["credentials"])
        elif preset["instanceId"] in before:
            preset.pop("credentials", None)
    return desired


def changed_paths(before, after, prefix=""):
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                paths.append(path)
            else:
                paths.extend(changed_paths(before[key], after[key], path))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths = []
        for index in range(max(len(before), len(after))):
            path = f"{prefix}.{index}"
            if index >= len(before) or index >= len(after):
                paths.append(path)
            else:
                paths.extend(changed_paths(before[index], after[index], path))
        return paths
    return [prefix] if before != after else []


def safe_label(value):
    if isinstance(value, str) and ("://" in value or value.startswith("//")):
        return "[redacted]"
    return value


def unique_presets(presets):
    indexed = {}
    for preset in presets:
        identifier = preset["instanceId"]
        if identifier in indexed:
            raise SystemExit(
                "duplicate preset instanceId: " + str(safe_label(identifier))
            )
        indexed[identifier] = preset
    return indexed


def validate_catalog_modifications(catalogs):
    seen = set()
    for catalog in catalogs:
        identity = (catalog["id"], catalog["type"])
        if identity in seen:
            raise SystemExit(
                "duplicate catalog (id, type): "
                + repr(tuple(safe_label(item) for item in identity))
            )
        seen.add(identity)


def merge_presets(current, template):
    current_by_id = unique_presets(current)
    unique_presets(template)
    conflicts = []
    for index, preset in enumerate(template):
        existing = current_by_id.get(preset["instanceId"])
        if existing is not None:
            conflicts.extend(
                changed_paths(
                    without_credentials(existing),
                    without_credentials(preset),
                    f"presets.{index}",
                )
            )
    if conflicts:
        raise SystemExit(
            "preset conflicts: "
            + ", ".join(str(safe_label(path)) for path in conflicts)
        )
    merged = copy.deepcopy(current)
    for index, preset in enumerate(template):
        identifier = preset["instanceId"]
        if identifier in current_by_id:
            continue
        positions = {
            item["instanceId"]: position for position, item in enumerate(merged)
        }
        following = next(
            (
                item["instanceId"]
                for item in template[index + 1 :]
                if item["instanceId"] in positions
            ),
            None,
        )
        preceding = next(
            (
                item["instanceId"]
                for item in reversed(template[:index])
                if item["instanceId"] in positions
            ),
            None,
        )
        position = (
            positions[following]
            if following is not None
            else positions[preceding] + 1
            if preceding is not None
            else len(merged)
        )
        merged.insert(position, copy.deepcopy(preset))
    return merged


def apply_rows(current, template):
    catalogs = template.get("catalogModifications") or []
    validate_catalog_modifications(catalogs)
    desired = copy.deepcopy(current)
    desired["presets"] = merge_presets(
        current.get("presets") or [], template.get("presets") or []
    )
    desired["catalogModifications"] = copy.deepcopy(catalogs)
    return desired


def row_state(config):
    return {
        key: config[key] for key in ("presets", "catalogModifications") if key in config
    }


def rows_sha256(config):
    return hash_state(config, ("presets", "catalogModifications"))


def catalog_tuples(config):
    return [
        tuple(safe_label(catalog.get(key)) for key in ("id", "type", "name", "enabled"))
        for catalog in config.get("catalogModifications") or []
    ]


def preset_differences(current, desired):
    before = unique_presets(current.get("presets") or [])
    after = unique_presets(desired.get("presets") or [])
    added = [identifier for identifier in after if identifier not in before]
    removed = [identifier for identifier in before if identifier not in after]
    paths = []
    for identifier in before:
        if identifier in after:
            paths.extend(
                changed_paths(
                    before[identifier], after[identifier], f"presets[{identifier}]"
                )
            )
    return added, removed, paths


def print_rows_diff(current, desired):
    for label, config in (("before", current), ("after", desired)):
        print(
            f"presets {label}: "
            + repr(
                [
                    safe_label(preset["instanceId"])
                    for preset in config.get("presets") or []
                ]
            )
        )
    added, removed, preset_paths = preset_differences(current, desired)
    print("presets added: " + repr([safe_label(identifier) for identifier in added]))
    print(
        "presets removed: " + repr([safe_label(identifier) for identifier in removed])
    )
    before = {key: value for key, value in current.items() if key != "presets"}
    after = {key: value for key, value in desired.items() if key != "presets"}
    for path in preset_paths + changed_paths(before, after):
        print("changed: " + str(safe_label(path)))
    print("catalogModifications before: " + repr(catalog_tuples(current)))
    print("catalogModifications after: " + repr(catalog_tuples(desired)))
    print("expect_sha256=" + rows_sha256(current))


def catalog_names(config):
    return [
        catalog.get("name") or catalog["id"]
        for catalog in config.get("catalogModifications") or []
    ]


def print_sync_diff(current, desired, template):
    for key in template:
        state = "same" if key in current and current[key] == desired[key] else "changed"
        print(f"{safe_label(key)}: {state}")
    added, removed, paths = preset_differences(current, desired)
    print("presets added: " + repr([safe_label(identifier) for identifier in added]))
    print(
        "presets removed: " + repr([safe_label(identifier) for identifier in removed])
    )
    for path in paths:
        print("changed: " + str(safe_label(path)))
    print("catalogModifications before: " + repr(catalog_tuples(current)))
    print("catalogModifications after: " + repr(catalog_tuples(desired)))
    print("expect_sha256=" + sync_sha256(current, template))


def verify_config(observed, desired, before, is_main):
    if canonical_config(observed) != canonical_config(desired):
        raise SystemExit("full configuration verification failed")
    before_presets = unique_presets(before.get("presets") or [])
    for preset in observed.get("presets") or []:
        existing = before_presets.get(preset["instanceId"])
        if existing is not None and (
            ("credentials" in preset) != ("credentials" in existing)
            or preset.get("credentials") != existing.get("credentials")
        ):
            raise SystemExit("preset credential verification failed")
    if not is_main and (
        ("jellyfin" in observed) != ("jellyfin" in before)
        or observed.get("jellyfin") != before.get("jellyfin")
    ):
        raise SystemExit("standalone Jellyfin verification failed")


def rollback_config(http, headers, before, target, path, error):
    try:
        http.call("PUT", "/api/v1/user", {"config": before}, headers=headers)
        restored = reconciliation_get(http, headers)["data"]["userData"]
        verify_config(restored, before, before, target[3])
    except (RequestError, SystemExit) as rollback_error:
        command = f"python3 ops/config-snapshot.py restore --config {target[0]} {shlex.quote(str(path))}"
        raise SystemExit(
            f"{error}; rollback failed: {rollback_error}; emergency snapshot: {path}\n{command}"
        ) from None
    raise SystemExit(f"{error}; rollback verified; emergency snapshot: {path}")


def guarded_write(http, headers, current, desired, target, directory=None):
    path = snapshot_path(
        directory or os.environ.get("SNAPSHOT_DIR", "~/media-gateway-restore"),
        target[0],
    )
    save_snapshot(path, current, target, Settings(ROOT).get("MDBLIST_API_KEY"))
    print(path)
    try:
        http.call("PUT", "/api/v1/user", {"config": desired}, headers=headers)
    except RequestError as error:
        try:
            updated = reconciliation_get(http, headers)
        except RequestError as get_error:
            rollback_config(
                http,
                headers,
                current,
                target,
                path,
                f"{error}; reconciliation failed: {get_error}",
            )
        observed = updated["data"]["userData"]
        if canonical_config(observed) != canonical_config(desired):
            if canonical_config(observed) == canonical_config(current):
                raise SystemExit(
                    f"{error}; no change was made; emergency snapshot: {path}"
                ) from None
            rollback_config(
                http,
                headers,
                current,
                target,
                path,
                f"{error}; reconciliation found an unexpected configuration",
            )
    else:
        try:
            updated = reconciliation_get(http, headers)
        except RequestError as error:
            rollback_config(
                http,
                headers,
                current,
                target,
                path,
                f"verification GET failed: {error}",
            )
    try:
        verify_config(updated["data"]["userData"], desired, current, target[3])
    except SystemExit as error:
        rollback_config(http, headers, current, target, path, error)
    return updated


def converge_config(
    http,
    headers,
    household,
    template,
    apply_template_keys,
    plan,
    response,
    apply_row_keys=False,
    diff=False,
    expect_sha256=None,
    sync_template_keys=False,
    allow_preset_removal=False,
    target=("main", "", "", True),
    snapshot_directory=None,
):
    current = response["data"]["userData"]
    guarded = apply_row_keys or sync_template_keys
    digest = (
        sync_sha256(current, template) if sync_template_keys else rows_sha256(current)
    )
    if guarded and not plan.dry_run and expect_sha256 != digest:
        raise SystemExit("--expect-sha256 does not match the current configuration")
    desired = (
        sync_template(current, template, allow_preset_removal)
        if sync_template_keys
        else apply_rows(current, template)
        if apply_row_keys
        else apply_template(current, template)
        if apply_template_keys
        else copy.deepcopy(current)
    )
    if target[3]:
        desired["jellyfin"] = desired_jellyfin(household, current.get("jellyfin") or {})
    elif "jellyfin" in current:
        desired["jellyfin"] = copy.deepcopy(current["jellyfin"])
    else:
        desired.pop("jellyfin", None)
    if diff:
        if sync_template_keys:
            print_sync_diff(current, desired, template)
        else:
            print_rows_diff(current, desired)
    if canonical_config(current) == canonical_config(desired):
        plan.ok("configuration " + target[0])
        return response
    if plan.change("update configuration " + target[0]):
        if guarded:
            return guarded_write(
                http, headers, current, desired, target, snapshot_directory
            )
        http.call("PUT", "/api/v1/user", {"config": desired}, headers=headers)
        return http.call("GET", "/api/v1/user", headers=headers)
    return response


def create_config(http, household, template, plan):
    desired = copy.deepcopy(template)
    desired["jellyfin"] = desired_jellyfin(household, {})
    if not plan.change("create primary AIOStreams configuration"):
        return None
    password = secrets.token_urlsafe(32)
    response = http.call(
        "POST", "/api/v1/user", {"config": desired, "password": password}
    )
    data = response.get("data") or {}
    uuid = data.get("uuid")
    encrypted_password = data.get("encryptedPassword")
    if not uuid or not encrypted_password:
        raise SystemExit(
            "create response did not contain data.uuid and data.encryptedPassword"
        )
    print(f"AIOSTREAMS_UUID={uuid}")
    print(f"AIOSTREAMS_PASSWORD={password}")
    print(
        "Store these values in .env; the plaintext password is not retrievable later."
    )
    return uuid, password, encrypted_password


def print_urls(url, uuid, encrypted_password):
    root = url.rstrip("/")
    print(f"server_url={root}/jellyfin")
    print(f"picker_url={root}/jellyfin/{uuid}/{encrypted_password}")
    print(
        f"stremio_manifest_url={root}/stremio/{uuid}/{encrypted_password}/manifest.json"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Create or converge an AIOStreams household configuration."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--apply-template",
        action="store_true",
        help="apply tuned template keys to an existing configuration",
    )
    modes.add_argument(
        "--apply-rows",
        action="store_true",
        help="merge missing presets and replace home row settings",
    )
    modes.add_argument(
        "--sync-template",
        action="store_true",
        help="sync template settings and preserve credentials",
    )
    parser.add_argument("--config", default="main", help="configuration name")
    parser.add_argument("--allow-preset-removal", action="store_true")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="print changed paths, catalog tuples, and the current configuration hash",
    )
    parser.add_argument(
        "--expect-sha256",
        help="required current configuration hash for --apply-rows or --sync-template writes",
    )
    parser.add_argument(
        "--print-urls",
        action="store_true",
        help="print the server URL and the credential-bearing picker and manifest URLs",
    )
    args = parser.parse_args()

    guarded = args.apply_rows or args.sync_template
    if args.diff and not guarded:
        parser.error("--diff requires --apply-rows or --sync-template")
    if guarded and not args.dry_run and not args.expect_sha256:
        parser.error(
            "--apply-rows and --sync-template require --expect-sha256 for writes"
        )
    if (guarded or args.diff or args.config != "main") and args.print_urls:
        parser.error(
            "--print-urls cannot be used with this mode or a standalone config"
        )
    if args.allow_preset_removal and not args.sync_template:
        parser.error("--allow-preset-removal requires --sync-template")
    settings = Settings(ROOT)
    target = resolve_target(settings, args.config)
    name, uuid, password, is_main = target
    if guarded and not uuid.strip():
        parser.error(
            "--apply-rows and --sync-template require an existing configuration"
        )
    household = {}
    if is_main:
        household_path = ROOT / "config" / "household.json"
        if not household_path.is_file():
            raise SystemExit(
                "config/household.json is missing; copy config/household.example.json first"
            )
        household = json.loads(household_path.read_text())
    url = (args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True)).rstrip("/")
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    template = configured_template(
        settings.get("TORBOX_API_KEY", required=True) if not uuid else None,
        settings,
    )
    print("config " + name)
    plan = Plan(args.dry_run)
    http = QuietHttp(url)
    aiostreams_session(http, auth)

    if not uuid:
        if password:
            raise SystemExit("AIOSTREAMS_PASSWORD is set but AIOSTREAMS_UUID is empty")
        created = create_config(http, household, template, plan)
        if created and args.print_urls:
            print_urls(url, created[0], created[2])
        verb = "pending" if plan.dry_run else "applied"
        print(f"{plan.changes} change(s) {verb}")
        return
    if not password:
        raise SystemExit("AIOSTREAMS_PASSWORD is required when AIOSTREAMS_UUID is set")

    headers = basic(uuid, password)
    response = http.call("GET", "/api/v1/user", headers=headers)
    response = converge_config(
        http,
        headers,
        household,
        template,
        args.apply_template,
        plan,
        response,
        args.apply_rows,
        args.diff,
        args.expect_sha256,
        args.sync_template,
        args.allow_preset_removal,
        target,
    )
    if args.print_urls:
        print_urls(url, uuid, response["data"]["encryptedPassword"])
    verb = "pending" if plan.dry_run else "applied"
    print(f"{plan.changes} change(s) {verb}")


if __name__ == "__main__":
    try:
        main()
    except RequestError as error:
        raise SystemExit(str(error)) from None
