#!/usr/bin/env python3
import argparse
import copy
import fcntl
import hashlib
import os
import re
from contextlib import contextmanager, nullcontext
from pathlib import Path

from bootstrap import guarded_write
from common import (
    QuietHttp,
    RequestError,
    Settings,
    aiostreams_session,
    basic,
    canonical_config,
    reconciliation_get,
    resolve_target,
)

ROOT = Path(__file__).resolve().parent.parent


def config_sha256(config):
    return hashlib.sha256(canonical_config(config).encode()).hexdigest()


def preference(config):
    if not isinstance(config, dict) or not isinstance(config.get("jellyfin"), dict):
        raise SystemExit("existing Jellyfin configuration is required")
    value = config["jellyfin"].get("resolveOnOpen")
    if not isinstance(value, bool):
        raise SystemExit("existing jellyfin.resolveOnOpen must be a boolean")
    return value


@contextmanager
def writer_lock(root):
    descriptor = os.open(
        root / ".stack.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("configuration writer lock is busy") from None
        yield
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Tune the existing main title-open preference with full verification."
    )
    parser.add_argument("--config", choices=("main",), default="main")
    parser.add_argument("--resolve-on-open", choices=("true", "false"), default="false")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--expect-sha256", help="frozen full-config hash from a prior dry run"
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="existing host settings root"
    )
    parser.add_argument(
        "--snapshot-dir", type=Path, help="private directory outside the stack"
    )
    parser.add_argument(
        "--writer-exclusion",
        action="store_true",
        help="confirm other configuration writers are paused; final GET/PUT still has an external-writer race",
    )
    args = parser.parse_args(argv)
    if args.expect_sha256 is not None and not re.fullmatch(
        r"[0-9a-fA-F]{64}", args.expect_sha256
    ):
        parser.error("--expect-sha256 must contain 64 hexadecimal characters")
    if args.apply and (not args.expect_sha256 or not args.writer_exclusion):
        parser.error("--apply requires --expect-sha256 and --writer-exclusion")
    root = args.root.expanduser().resolve()
    directory = (
        args.snapshot_dir
        or Path(os.environ.get("SNAPSHOT_DIR", "~/media-gateway-restore"))
    ).expanduser()
    if directory.resolve().is_relative_to(root) or directory.resolve().is_relative_to(
        ROOT
    ):
        parser.error(
            "snapshot directory must be outside the settings and staged sync trees"
        )
    settings = Settings(root)
    target = resolve_target(settings, args.config)
    if not target[1].strip() or not target[2].strip():
        raise SystemExit("configuration credentials are required")
    auth = settings.get("AIOSTREAMS_AUTH", required=True)
    http = QuietHttp(settings.get("AIOSTREAMS_PUBLIC_URL", required=True))
    headers = basic(target[1], target[2])
    with writer_lock(root) if args.apply else nullcontext():
        aiostreams_session(http, auth)
        current = reconciliation_get(http, headers)["data"]["userData"]
        old = preference(current)
        digest = config_sha256(current)
        desired_value = args.resolve_on_open == "true"
        print(
            f"jellyfin.resolveOnOpen: {str(old).lower()} -> {str(desired_value).lower()}"
        )
        print("expect_sha256=" + digest)
        if not args.apply:
            return
        if digest != args.expect_sha256.lower():
            raise SystemExit("full configuration precondition does not match")
        desired = copy.deepcopy(current)
        desired["jellyfin"]["resolveOnOpen"] = desired_value
        latest = reconciliation_get(http, headers)["data"]["userData"]
        preference(latest)
        if config_sha256(latest) != args.expect_sha256.lower():
            raise SystemExit("full configuration changed before write")
        if old == desired_value:
            return
        guarded_write(
            http, headers, current, desired, target, directory, settings=settings
        )


if __name__ == "__main__":
    try:
        main()
    except RequestError as error:
        raise SystemExit(str(error)) from None
    except OSError:
        raise SystemExit("local settings, lock or snapshot operation failed") from None
