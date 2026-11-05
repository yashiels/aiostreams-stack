import copy
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rehearse_browse_tuning import DEPENDENCIES, ROOT


class InterruptionTests(unittest.TestCase):
    def test_committed_interruption_stale_hash_refusal_and_fresh_noop(self):
        with tempfile.TemporaryDirectory(prefix="browse-interruption-") as directory:
            run = Path(directory)
            staged = run / "staged" / "ops"
            staged.mkdir(parents=True, mode=0o700)
            settings = run / "settings"
            settings.mkdir(mode=0o700)
            snapshots = run / "snapshots"
            for name in DEPENDENCIES:
                shutil.copyfile(ROOT / "ops" / name, staged / name)
            original = {
                "jellyfin": {"resolveOnOpen": True, "personas": [{"id": "fake"}]},
                "presets": [
                    {"instanceId": "fake", "credentials": {"token": "fake-token"}}
                ],
                "custom": {"keep": [1, 2]},
            }
            desired = copy.deepcopy(original)
            desired["jellyfin"]["resolveOnOpen"] = False
            state = {"config": copy.deepcopy(original), "puts": 0, "block": False}
            committed_readback = threading.Event()
            release_readback = threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def reply(self, body):
                    payload = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_POST(self):
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.reply({"success": True})

                def do_GET(self):
                    if state["block"] and state["puts"] == 1:
                        committed_readback.set()
                        release_readback.wait(10)
                        return
                    self.reply({"data": {"userData": state["config"]}})

                def do_PUT(self):
                    body = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    state["config"] = body["config"]
                    state["puts"] += 1
                    self.reply({"success": True})

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            process = None
            try:
                env_file = settings / ".env"
                env_file.write_text(
                    f"AIOSTREAMS_PUBLIC_URL=http://127.0.0.1:{server.server_port}\n"
                    "AIOSTREAMS_AUTH=fake:fake-password\n"
                    "AIOSTREAMS_UUID=fake-id\nAIOSTREAMS_PASSWORD=fake-password\n"
                )
                env_file.chmod(0o600)
                command = [
                    sys.executable,
                    str(staged / "browse-tuning.py"),
                    "--root",
                    str(settings),
                    "--snapshot-dir",
                    str(snapshots),
                ]
                environment = {
                    "PATH": os.environ.get("PATH", ""),
                    "TMPDIR": str(run),
                    "M0_TEST_SCRATCH": str(run),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }

                def execute(arguments):
                    return subprocess.run(
                        [*command, *arguments],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )

                initial = execute(["--dry-run"])
                self.assertEqual(initial.returncode, 0, initial.stderr)
                frozen = re.search(
                    r"expect_sha256=([0-9a-f]{64})", initial.stdout
                ).group(1)
                self.assertFalse(snapshots.exists())
                state["block"] = True
                process = subprocess.Popen(
                    [
                        *command,
                        "--apply",
                        "--expect-sha256",
                        frozen,
                        "--writer-exclusion",
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertTrue(
                    committed_readback.wait(10),
                    "server did not reach committed readback barrier",
                )
                self.assertEqual(state["config"], desired)
                self.assertEqual(state["puts"], 1)
                snapshot = next(snapshots.glob("*.json"))
                retained = snapshot.read_bytes()
                self.assertEqual(json.loads(retained)["config"], original)
                self.assertEqual(
                    json.loads(retained)["target"], {"name": "main", "uuid": "fake-id"}
                )
                process.terminate()
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, -signal.SIGTERM)
                state["block"] = False
                release_readback.set()
                with (settings / ".stack.lock").open("r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stale = execute(
                    ["--apply", "--expect-sha256", frozen, "--writer-exclusion"]
                )
                self.assertNotEqual(stale.returncode, 0)
                self.assertIn("precondition does not match", stale.stderr)
                self.assertEqual(state["puts"], 1)
                fresh = execute(["--dry-run"])
                self.assertEqual(fresh.returncode, 0, fresh.stderr)
                self.assertIn("false -> false", fresh.stdout)
                new_hash = re.search(
                    r"expect_sha256=([0-9a-f]{64})", fresh.stdout
                ).group(1)
                self.assertNotEqual(new_hash, frozen)
                noop = execute(
                    ["--apply", "--expect-sha256", new_hash, "--writer-exclusion"]
                )
                self.assertEqual(noop.returncode, 0, noop.stderr)
                self.assertEqual(state["puts"], 1)
                self.assertEqual(state["config"], desired)
                self.assertEqual(snapshot.read_bytes(), retained)
                self.assertEqual(len(list(snapshots.glob("*.json"))), 1)
                print(
                    json.dumps(
                        {
                            "proof": "real staged CLI interrupted at committed readback barrier",
                            "interrupted_exit": process.returncode,
                            "stale_hash_exit": stale.returncode,
                            "fresh_hash_noop_exit": noop.returncode,
                            "puts": state["puts"],
                            "snapshot_preserved": True,
                            "lock_reacquired": True,
                        }
                    )
                )
            finally:
                release_readback.set()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
