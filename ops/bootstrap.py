#!/usr/bin/env python3
import argparse
import copy
import json
import re
import secrets
import urllib.parse
from pathlib import Path

from common import Http, Plan, Settings, aiostreams_session, basic

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


def catalog_names(config):
    return [catalog.get("name") or catalog["id"] for catalog in config.get("catalogModifications") or []]


def converge_config(http, headers, household, template, apply_template_keys, plan, response):
    current = response["data"]["userData"]
    desired = apply_template(current, template) if apply_template_keys else copy.deepcopy(current)
    desired["jellyfin"] = desired_jellyfin(household, current.get("jellyfin") or {})
    names = catalog_names(desired)
    print(f"note catalog order: {', '.join(names) if names else '(none)'}")
    template_matches = not apply_template_keys or all(current.get(key) == desired.get(key) for key in template)
    jellyfin_matches = managed_jellyfin(current.get("jellyfin") or {}) == managed_jellyfin(desired["jellyfin"])
    if jellyfin_matches and template_matches:
        plan.ok("AIOStreams template and household personas")
        return response
    label = "update AIOStreams template and household personas" if apply_template_keys else "update AIOStreams household personas"
    if plan.change(label):
        http.call("PUT", "/api/v1/user", {"config": desired}, headers=headers)
        return http.call("GET", "/api/v1/user", headers=headers)
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
    print(f"server_url={url.rstrip('/')}/jellyfin")
    print(f"picker_url={url.rstrip('/')}/jellyfin/{uuid}/{encrypted_password}")


def main():
    parser = argparse.ArgumentParser(description="Create or converge an AIOStreams household configuration.")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL")
    parser.add_argument("--apply-template", action="store_true", help="apply tuned template keys to an existing configuration")
    parser.add_argument("--print-urls", action="store_true", help="print the server and credential-bearing picker URLs")
    args = parser.parse_args()

    household_path = ROOT / "config" / "household.json"
    if not household_path.is_file():
        raise SystemExit("config/household.json is missing; copy config/household.example.json first")
    household = json.loads(household_path.read_text())
    settings = Settings(ROOT)
    url = (args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True)).rstrip("/")
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    uuid = settings.get("AIOSTREAMS_UUID")
    password = settings.get("AIOSTREAMS_PASSWORD")
    template = configured_template(settings.get("TORBOX_API_KEY", required=True))
    plan = Plan(args.dry_run)
    http = Http(url)
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
    response = converge_config(http, headers, household, template, args.apply_template, plan, response)
    if args.print_urls:
        print_urls(url, uuid, response["data"]["encryptedPassword"])
    verb = "pending" if plan.dry_run else "applied"
    print(f"{plan.changes} change(s) {verb}")


if __name__ == "__main__":
    main()
