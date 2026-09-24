#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

REQUIRED = [
    "AIOSTREAMS_PUBLIC_URL",
    "AIOSTREAMS_SECRET_KEY",
    "AIOSTREAMS_AUTH",
    "AIOSTREAMS_CONFIG_ACCESS_KEY",
    "TORBOX_API_KEY",
    "TMDB_API_KEY",
    "MEDIA_GATEWAY_TUNNEL_TOKEN",
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
]

if __name__ == "__main__":
    env_file = sys.argv[1]
    exporter = Path(__file__).with_name("env-export.py")
    out = subprocess.run(
        [sys.executable, str(exporter), env_file],
        capture_output=True, text=True, check=True,
    ).stdout

    decoded = {}
    for line in out.splitlines():
        if not line.startswith("export "):
            continue
        key, _, value = line[len("export "):].partition("=")
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("'\"'\"'", "'")
        decoded[key] = value

    missing = [k for k in REQUIRED if not decoded.get(k, "").strip()]
    if missing:
        raise SystemExit(f"env is missing or empty for: {', '.join(missing)}")
    print(f"env ok: {len(decoded)} keys, {len(REQUIRED)} required present")
