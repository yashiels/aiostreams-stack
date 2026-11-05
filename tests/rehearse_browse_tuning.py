#!/usr/bin/env python3
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops"))
import mdblist

DEPENDENCIES = (
    "browse-tuning.py",
    "bootstrap.py",
    "common.py",
    "mdblist.py",
    "env-export.py",
)


def rehearse(scratch_root, deliberate_failure=False):
    metadata = scratch_root.lstat()
    if (
        not scratch_root.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise SystemExit("scratch root must be an absolute private owned directory")
    run = Path(tempfile.mkdtemp(prefix="browse-rehearsal-", dir=scratch_root))
    staged = run / "staged" / "ops"
    staged.mkdir(parents=True)
    settings = run / "settings"
    settings.mkdir()
    for name in DEPENDENCIES:
        shutil.copyfile(ROOT / "ops" / name, staged / name)
    original = {
        "jellyfin": {
            "resolveOnOpen": True,
            "personas": [{"id": "test", "custom": [1]}],
        },
        "custom": {"keep": True},
        "presets": [
            {"instanceId": "fake", "credentials": {"token": "fake-secret"}},
            {
                "instanceId": "gac",
                "type": "custom",
                "options": {
                    "manifestUrl": f"https://{mdblist.HOST}/3106/fake-mdblist-key/manifest.json"
                },
            },
        ],
        "services": [{"id": "fake", "credential": "fake-service-secret"}],
    }
    state = {"config": copy.deepcopy(original), "puts": 0, "logins": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            assert self.path == "/api/v1/auth/login"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {"username": "test", "password": "fake-admin-password"}
            state["logins"] += 1
            self.reply({"success": True})

        def do_GET(self):
            assert self.path == "/api/v1/user"
            self.reply({"data": {"userData": state["config"]}})

        def do_PUT(self):
            assert self.path == "/api/v1/user"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["config"] = body["config"]
            state["puts"] += 1
            self.reply({"success": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = settings / ".env"
        env_file.write_text(
            f"AIOSTREAMS_PUBLIC_URL=http://127.0.0.1:{server.server_port}\n"
            "AIOSTREAMS_AUTH=test:fake-admin-password\n"
            "AIOSTREAMS_UUID=fake-id\nAIOSTREAMS_PASSWORD=fake-user-password\n"
            "MDBLIST_API_KEY=fake-mdblist-key\n"
        )
        env_file.chmod(0o600)
        snapshots = run / "snapshots"
        transcript = []

        def execute(*arguments):
            result = subprocess.run(
                [
                    sys.executable,
                    str(staged / "browse-tuning.py"),
                    "--root",
                    str(settings),
                    "--snapshot-dir",
                    str(snapshots),
                    *arguments,
                ],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "TMPDIR": str(run),
                    "M0_TEST_SCRATCH": str(run),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            transcript.append(
                {
                    "arguments": list(arguments),
                    "status": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            assert result.returncode == 0, "CLI execution failed"
            for secret in (
                "fake-secret",
                "fake-admin-password",
                "fake-user-password",
                "fake-id",
                "http://",
            ):
                assert secret not in result.stdout + result.stderr, (
                    "CLI output exposed a fixture secret"
                )
            return re.search(r"expect_sha256=([0-9a-f]{64})", result.stdout).group(1)

        digest = execute("--dry-run")
        assert state["puts"] == 0 and not snapshots.exists()
        execute("--apply", "--expect-sha256", digest, "--writer-exclusion")
        desired = copy.deepcopy(original)
        desired["jellyfin"]["resolveOnOpen"] = False
        assert state["config"] == desired and state["puts"] == 1
        assert len(list(snapshots.glob("*.json"))) == 1
        repeat_digest = execute("--dry-run")
        execute("--apply", "--expect-sha256", repeat_digest, "--writer-exclusion")
        assert state["puts"] == 1 and len(list(snapshots.glob("*.json"))) == 1
        restore_digest = execute("--dry-run", "--resolve-on-open", "true")
        execute(
            "--apply",
            "--resolve-on-open",
            "true",
            "--expect-sha256",
            restore_digest,
            "--writer-exclusion",
        )
        assert state["config"] == original and state["puts"] == 2
        assert state["logins"] == 6
        assert len(list(snapshots.glob("*.json"))) == 2
        for snapshot in snapshots.glob("*.json"):
            assert snapshot.stat().st_mode & 0o777 == 0o600
            body = json.loads(snapshot.read_text())
            assert body["target"] == {"name": "main", "uuid": "fake-id"}
            assert "fake-mdblist-key" not in snapshot.read_text()
            assert mdblist.TOKEN in snapshot.read_text()
        (run / "evidence.json").write_text(
            json.dumps(
                {
                    "transcript": transcript,
                    "puts": state["puts"],
                    "logins": state["logins"],
                    "restored": state["config"] == original,
                    "staged_sha256": {
                        name: hashlib.sha256((staged / name).read_bytes()).hexdigest()
                        for name in DEPENDENCIES
                    },
                },
                indent=2,
            )
            + "\n"
        )
        print("rehearsal=" + str(run))
        if deliberate_failure:
            assert state["puts"] == 999, "intentional assertion failure"
        print(
            "PASS: dry run, frozen hash, apply, idempotency, boolean restoration; 2 PUTs, 6 logins"
        )
        return run
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser(
        description="Rehearse the staged browse CLI against an isolated fake HTTP service."
    )
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--deliberate-failure", action="store_true")
    args = parser.parse_args()
    rehearse(args.scratch_root, args.deliberate_failure)


if __name__ == "__main__":
    main()
