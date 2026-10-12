#!/usr/bin/env python3
import argparse
import json
import urllib.parse
from pathlib import Path

from common import (
    QuietHttp,
    RequestError,
    Settings,
    aiostreams_session,
    basic,
    resolve_target,
)

ROOT = Path(__file__).resolve().parent.parent
EMBY_AUTHORIZATION = 'MediaBrowser Client="check-catalogs", Device="check-catalogs", DeviceId="check-catalogs", Version="1"'


def safe_label(value):
    return (
        "[redacted]"
        if isinstance(value, str) and ("://" in value or value.startswith("//"))
        else value
    )


def manifest_entries(catalogs, label):
    seen = set()
    entries = set()
    for catalog in catalogs:
        identity = (catalog["type"], catalog["id"])
        if identity in seen:
            raise SystemExit(
                f"duplicate {label} catalog: "
                + repr(tuple(safe_label(item) for item in identity))
            )
        seen.add(identity)
        entries.add((*identity, catalog["name"]))
    return entries


def catalog_differences(fixture, manifest, views):
    expected = manifest_entries(fixture["manifest"], "fixture")
    observed = manifest_entries(manifest["catalogs"], "observed")
    differences = []
    for label, entries in (
        ("missing", expected - observed),
        ("unexpected", observed - expected),
    ):
        differences.extend(
            f"{label} catalog: " + repr(tuple(safe_label(item) for item in entry))
            for entry in sorted(entries)
        )
    if fixture["views"] != views:
        differences.append(
            "expected views: " + repr([safe_label(name) for name in fixture["views"]])
        )
        differences.append(
            "observed views: " + repr([safe_label(name) for name in views])
        )
    return differences


def main():
    parser = argparse.ArgumentParser(
        description="Check the manifest catalogs and ordered Jellyfin home rows."
    )
    parser.add_argument(
        "--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL"
    )
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "config" / "expected-catalogs.json"
    )
    parser.add_argument("--config", default="main", help="configuration name")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    manifest_entries(fixture["manifest"], "fixture")
    settings = Settings(ROOT)
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    name, uuid, password, _ = resolve_target(settings, args.config)
    if not uuid or not password:
        raise SystemExit("configuration credentials are required")
    http = QuietHttp(args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True))
    aiostreams_session(http, auth)
    data = http.call("GET", "/api/v1/user", headers=basic(uuid, password))["data"]
    manifest_path = "/stremio/{}/{}/manifest.json".format(
        urllib.parse.quote(uuid, safe=""),
        urllib.parse.quote(data["encryptedPassword"], safe=""),
    )
    manifest = http.call("GET", manifest_path)
    session = http.call(
        "POST",
        "/jellyfin/Users/AuthenticateByName",
        {"Username": uuid, "Pw": password},
        headers={"X-Emby-Authorization": EMBY_AUTHORIZATION},
    )
    user_id = urllib.parse.quote(session["User"]["Id"], safe="")
    response = http.call(
        "GET",
        f"/jellyfin/Users/{user_id}/Views",
        headers={"X-Emby-Token": session["AccessToken"]},
    )
    differences = catalog_differences(
        fixture, manifest, [item["Name"] for item in response["Items"]]
    )
    if differences:
        print("config " + name)
        print("\n".join(differences))
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except RequestError as error:
        raise SystemExit(str(error)) from None
