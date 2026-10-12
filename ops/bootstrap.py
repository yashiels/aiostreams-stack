#!/usr/bin/env python3
import argparse
import copy
import hashlib
import json
import re
import secrets
import urllib.parse
from pathlib import Path

from common import Http, Plan, QuietHttp, Settings, aiostreams_session, basic

ROOT = Path(__file__).resolve().parent.parent
PERSONA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MANAGED_PERSONA_FIELDS = ("id", "name", "avatar", "history")
SECRET_KEY = re.compile(r"key|token|password|secret|credential", re.IGNORECASE)


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
        preserved = {key: value for key, value in existing.items() if key not in MANAGED_PERSONA_FIELDS}
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
        "personas": [managed_persona(persona) for persona in jellyfin.get("personas") or []],
    }


def configured_template(torbox_key):
    template = json.loads((ROOT / "config" / "aiostreams.template.json").read_text())
    services = template.get("services") or []
    torbox = next((service for service in services if service.get("id") == "torbox"), None)
    if torbox is None:
        raise SystemExit("template has no torbox service")
    torbox["credentials"] = {"apiKey": torbox_key}
    return template


def apply_template(current, template):
    desired = copy.deepcopy(current)
    for key, value in template.items():
        if key != "services":
            desired[key] = copy.deepcopy(value)
    current_services = {service.get("id"): service for service in current.get("services") or []}
    desired_services = []
    for template_service in template.get("services") or []:
        service = copy.deepcopy(template_service)
        existing = current_services.get(service.get("id"), {})
        service["credentials"] = copy.deepcopy(existing.get("credentials") or service.get("credentials") or {})
        desired_services.append(service)
    template_ids = {service.get("id") for service in desired_services}
    desired_services.extend(
        copy.deepcopy(service) for service in current.get("services") or [] if service.get("id") not in template_ids
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


def without_secrets(value):
    if isinstance(value, dict):
        return {
            key: without_secrets(item)
            for key, item in value.items()
            if not SECRET_KEY.search(key)
        }
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    return value


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
    return {key: config[key] for key in ("presets", "catalogModifications") if key in config}


def rows_sha256(config):
    encoded = json.dumps(
        without_secrets(row_state(config)), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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
            paths.extend(changed_paths(before[identifier], after[identifier], f"presets[{identifier}]"))
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
    print("presets removed: " + repr([safe_label(identifier) for identifier in removed]))
    before = {key: value for key, value in current.items() if key != "presets"}
    after = {key: value for key, value in desired.items() if key != "presets"}
    for path in preset_paths + changed_paths(before, after):
        print("changed: " + str(safe_label(path)))
    print("catalogModifications before: " + repr(catalog_tuples(current)))
    print("catalogModifications after: " + repr(catalog_tuples(desired)))
    print("expect_sha256=" + rows_sha256(current))


def catalog_names(config):
    return [catalog.get("name") or catalog["id"] for catalog in config.get("catalogModifications") or []]


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
):
    current = response["data"]["userData"]
    if apply_row_keys and not plan.dry_run and expect_sha256 != rows_sha256(current):
        raise SystemExit("--expect-sha256 does not match the current row configuration")
    desired = (
        apply_rows(current, template)
        if apply_row_keys
        else apply_template(current, template)
        if apply_template_keys
        else copy.deepcopy(current)
    )
    desired["jellyfin"] = desired_jellyfin(household, current.get("jellyfin") or {})
    if diff:
        print_rows_diff(current, desired)
    names = catalog_names(desired)
    if not diff and not apply_row_keys:
        print(f"note catalog order: {', '.join(names) if names else '(none)'}")
    template_matches = (
        row_state(current) == row_state(desired)
        if apply_row_keys
        else not apply_template_keys
        or all(current.get(key) == desired.get(key) for key in template)
    )
    jellyfin_matches = managed_jellyfin(
        current.get("jellyfin") or {}
    ) == managed_jellyfin(desired["jellyfin"])
    if jellyfin_matches and template_matches:
        plan.ok("AIOStreams template and household personas")
        return response
    label = (
        "update AIOStreams template and household personas"
        if apply_template_keys
        else "update AIOStreams household personas"
    )
    if apply_row_keys:
        label = "update AIOStreams home rows and household personas"
    if plan.change(label):
        http.call("PUT", "/api/v1/user", {"config": desired}, headers=headers)
        updated = http.call("GET", "/api/v1/user", headers=headers)
        if apply_row_keys and row_state(updated["data"]["userData"]) != row_state(
            desired
        ):
            raise SystemExit("home row verification failed after update")
        return updated
    return response


def create_config(http, household, template, plan):
    desired = copy.deepcopy(template)
    desired["jellyfin"] = desired_jellyfin(household, {})
    if not plan.change("create primary AIOStreams configuration"):
        return None
    password = secrets.token_urlsafe(32)
    response = http.call("POST", "/api/v1/user", {"config": desired, "password": password})
    data = response.get("data") or {}
    uuid = data.get("uuid")
    encrypted_password = data.get("encryptedPassword")
    if not uuid or not encrypted_password:
        raise SystemExit("create response did not contain data.uuid and data.encryptedPassword")
    print(f"AIOSTREAMS_UUID={uuid}")
    print(f"AIOSTREAMS_PASSWORD={password}")
    print("Store these values in .env; the plaintext password is not retrievable later.")
    return uuid, password, encrypted_password


def print_urls(url, uuid, encrypted_password):
    root = url.rstrip("/")
    print(f"server_url={root}/jellyfin")
    print(f"picker_url={root}/jellyfin/{uuid}/{encrypted_password}")
    print(f"stremio_manifest_url={root}/stremio/{uuid}/{encrypted_password}/manifest.json")


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
    parser.add_argument(
        "--diff", action="store_true", help="print row changes and the current row hash"
    )
    parser.add_argument(
        "--expect-sha256", help="required current row hash for --apply-rows writes"
    )
    parser.add_argument(
        "--print-urls",
        action="store_true",
        help="print the server URL and the credential-bearing picker and manifest URLs",
    )
    args = parser.parse_args()

    if args.diff and not args.apply_rows:
        parser.error("--diff requires --apply-rows")
    if args.apply_rows and not args.dry_run and not args.expect_sha256:
        parser.error("--apply-rows requires --expect-sha256 for writes")
    if (args.apply_rows or args.diff) and args.print_urls:
        parser.error("--print-urls cannot be used with --apply-rows or --diff")
    settings = Settings(ROOT)
    if (args.apply_rows or args.diff) and not settings.get("AIOSTREAMS_UUID").strip():
        parser.error("--apply-rows and --diff require an existing AIOSTREAMS_UUID")

    household_path = ROOT / "config" / "household.json"
    if not household_path.is_file():
        raise SystemExit(
            "config/household.json is missing; copy config/household.example.json first"
        )
    household = json.loads(household_path.read_text())
    url = (args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True)).rstrip("/")
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    uuid = settings.get("AIOSTREAMS_UUID")
    password = settings.get("AIOSTREAMS_PASSWORD")
    template = (
        json.loads((ROOT / "config" / "aiostreams.template.json").read_text())
        if args.apply_rows
        else configured_template(settings.get("TORBOX_API_KEY", required=True))
    )
    plan = Plan(args.dry_run)
    http = QuietHttp(url) if args.apply_rows or args.diff else Http(url)
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
    )
    if args.print_urls:
        print_urls(url, uuid, response["data"]["encryptedPassword"])
    verb = "pending" if plan.dry_run else "applied"
    print(f"{plan.changes} change(s) {verb}")


if __name__ == "__main__":
    main()
