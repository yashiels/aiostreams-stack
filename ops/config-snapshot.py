#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from common import (
    QuietHttp,
    RequestError,
    Settings,
    aiostreams_session,
    basic,
    canonical_config,
    private_directory,
    resolve_target,
    save_snapshot,
    snapshot_path,
)
from mdblist import restored_config

ROOT = Path(__file__).resolve().parent.parent


def restore_snapshot(http, headers, body):
    http.call("PUT", "/api/v1/user", {"config": body["config"]}, headers=headers)
    restored = http.call("GET", "/api/v1/user", headers=headers)["data"]["userData"]
    if canonical_config(restored) != canonical_config(body["config"]):
        raise SystemExit("snapshot verification failed after restore")


def load_snapshot(path, target):
    private_directory(path.parent)
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit("snapshot cannot be read as JSON") from None
    if (
        not isinstance(body, dict)
        or set(body) != {"target", "config"}
        or not isinstance(body["config"], dict)
        or body["target"] != {"name": target[0], "uuid": target[1]}
    ):
        raise SystemExit(
            "snapshot target must match the config name and UUID; legacy snapshots are rejected"
        )
    return body


def main():
    parser = argparse.ArgumentParser(
        description="Save or restore an AIOStreams configuration."
    )
    parser.add_argument(
        "--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL"
    )
    parser.add_argument("--config", default="main", help="configuration name")
    parser.add_argument("action", choices=("save", "restore"))
    parser.add_argument(
        "path", type=Path, nargs="?", help="save directory or restore file"
    )
    args = parser.parse_args()
    if args.action == "restore" and args.path is None:
        parser.error("restore requires a snapshot file")
    if args.action == "save" and args.path is not None:
        args.path = args.path.expanduser()
        if args.path.is_symlink() or (args.path.exists() and not args.path.is_dir()):
            raise SystemExit(f"snapshot already exists: {args.path}")
    settings = Settings(ROOT)
    target = resolve_target(settings, args.config)
    name, uuid, password, _ = target
    if not uuid or not password:
        raise SystemExit("configuration credentials are required")
    body = None
    if args.action == "restore":
        args.path = args.path.expanduser()
        body = load_snapshot(args.path, target)
        body["config"] = restored_config(
            body["config"], settings.get("MDBLIST_API_KEY")
        )
    else:
        args.path = snapshot_path(
            args.path or os.environ.get("SNAPSHOT_DIR", "~/media-gateway-restore"), name
        )
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    http = QuietHttp(args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True))
    headers = basic(uuid, password)
    aiostreams_session(http, auth)
    if args.action == "save":
        response = http.call("GET", "/api/v1/user", headers=headers)
        save_snapshot(
            args.path,
            response["data"]["userData"],
            target,
            settings.get("MDBLIST_API_KEY"),
        )
        print(args.path)
    else:
        restore_snapshot(http, headers, body)
        print("restored and verified config " + name)


if __name__ == "__main__":
    try:
        main()
    except RequestError as error:
        raise SystemExit(str(error)) from None
