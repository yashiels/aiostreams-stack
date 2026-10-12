#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from common import QuietHttp, Settings, aiostreams_session, basic

ROOT = Path(__file__).resolve().parent.parent
SERVER_MAINTAINED_KEYS = ("statistics",)


def canonical_config(config):
    return json.dumps(
        {
            key: value
            for key, value in config.items()
            if key not in SERVER_MAINTAINED_KEYS
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def save_snapshot(path, config):
    missing_parents = []
    parent = path.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    for parent in reversed(missing_parents):
        parent.mkdir(mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"snapshot already exists: {path}") from None
    with os.fdopen(descriptor, "w") as output:
        json.dump({"config": config}, output)
        output.write("\n")


def restore_snapshot(http, headers, body):
    http.call("PUT", "/api/v1/user", body, headers=headers)
    restored = http.call("GET", "/api/v1/user", headers=headers)["data"]["userData"]
    if canonical_config(restored) != canonical_config(body["config"]):
        raise SystemExit("snapshot verification failed after restore")


def main():
    parser = argparse.ArgumentParser(
        description="Save or restore an AIOStreams configuration."
    )
    parser.add_argument(
        "--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL"
    )
    parser.add_argument("action", choices=("save", "restore"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "save" and args.path.exists():
        raise SystemExit(f"snapshot already exists: {args.path}")
    settings = Settings(ROOT)
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    uuid = settings.get("AIOSTREAMS_UUID", required=True)
    password = settings.get("AIOSTREAMS_PASSWORD", required=True)
    http = QuietHttp(args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True))
    headers = basic(uuid, password)
    body = None
    if args.action == "restore":
        body = json.loads(args.path.read_text())
        if (
            not isinstance(body, dict)
            or set(body) != {"config"}
            or not isinstance(body["config"], dict)
        ):
            raise SystemExit("snapshot must contain exactly one config object")
    aiostreams_session(http, auth)
    if args.action == "save":
        response = http.call("GET", "/api/v1/user", headers=headers)
        save_snapshot(args.path, response["data"]["userData"])
        print("saved configuration snapshot")
    else:
        restore_snapshot(http, headers, body)
        print("restored and verified configuration snapshot")


if __name__ == "__main__":
    main()
