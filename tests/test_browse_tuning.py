import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import bootstrap
import common
from test_bootstrap_rows import HOUSEHOLD

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "browse_tuning", ROOT / "ops/browse-tuning.py"
)
browse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browse)
TARGET = ("main", "fake-id", "fake-password", True)
BEFORE = {
    "jellyfin": {
        "resolveOnOpen": True,
        "personas": [{"id": "test", "custom": {"keep": [1, 2]}}],
        "primary": {"custom": True},
    },
    "presets": [{"instanceId": "a", "credentials": {"token": "fake-secret"}}],
    "custom": {"nested": [1, 2]},
}


def response(config):
    return {"data": {"userData": copy.deepcopy(config)}}


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "settings"
        self.root.mkdir()
        self.snapshots = Path(self.temp.name) / "snapshots"
        self.settings = Mock()
        self.settings.get.side_effect = lambda key, required=False: {
            "AIOSTREAMS_AUTH": "test:fake-password",
            "AIOSTREAMS_PUBLIC_URL": "http://example.invalid",
            "MDBLIST_API_KEY": "fake-mdblist-key",
        }[key]
        self.http = Mock()
        self.output = io.StringIO()
        self.desired = copy.deepcopy(BEFORE)
        self.desired["jellyfin"]["resolveOnOpen"] = False

    def run_cli(self, arguments=(), states=None, target=TARGET):
        if states is not None:
            self.http.call.side_effect = states
        with (
            patch.object(browse, "Settings", return_value=self.settings),
            patch.object(browse, "resolve_target", return_value=target),
            patch.object(browse, "QuietHttp", return_value=self.http),
            contextlib.redirect_stdout(self.output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            browse.main(
                [
                    "--root",
                    str(self.root),
                    "--snapshot-dir",
                    str(self.snapshots),
                    *arguments,
                ]
            )

    def apply_args(self, config=BEFORE):
        return [
            "--apply",
            "--expect-sha256",
            browse.config_sha256(config),
            "--writer-exclusion",
        ]

    def test_default_and_explicit_dry_run_do_not_write_or_snapshot(self):
        for arguments in ([], ["--dry-run"]):
            self.http.reset_mock()
            self.run_cli(arguments, [{}, response(BEFORE)])
            self.assertEqual(
                [c.args[0] for c in self.http.call.call_args_list], ["POST", "GET"]
            )
            self.assertFalse(self.snapshots.exists())
            self.assertFalse((self.root / ".stack.lock").exists())
        for secret in ("fake-password", "fake-secret", "http://", "personas"):
            self.assertNotIn(secret, self.output.getvalue())

    def test_invalid_arguments_refuse_before_http(self):
        for arguments in (
            ["--config", "standalone"],
            ["--expect-sha256", "invalid"],
            ["--apply"],
            ["--apply", "--expect-sha256", "a" * 64],
            ["--apply", "--dry-run"],
            ["--snapshot-dir", str(self.root / "inside")],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.run_cli(arguments)
            self.http.call.assert_not_called()

    def test_absent_credentials_refuse_before_http(self):
        for target in (("main", "", "pw", True), ("main", "id", " ", True)):
            with self.assertRaisesRegex(SystemExit, "credentials"):
                self.run_cli(target=target)
            self.http.call.assert_not_called()

    def test_missing_auth_refuses_before_http(self):
        self.settings.get.side_effect = SystemExit("missing authentication")
        with self.assertRaisesRegex(SystemExit, "missing authentication"):
            self.run_cli()
        self.http.call.assert_not_called()

    def test_missing_or_malformed_preference_refuses_without_snapshot(self):
        for config in (
            {},
            {"jellyfin": None},
            {"jellyfin": []},
            {"jellyfin": {}},
            *(
                {"jellyfin": {"resolveOnOpen": value}}
                for value in (None, 0, 1, "false", [])
            ),
        ):
            with self.subTest(config=config), self.assertRaises(SystemExit):
                self.run_cli(self.apply_args(), [{}, response(config)])
            self.assertFalse(self.snapshots.exists())
            self.assertFalse(
                any(c.args[0] == "PUT" for c in self.http.call.call_args_list)
            )
            self.http.reset_mock()

    def test_full_precondition_and_last_read_drift_refuse(self):
        drift = {**BEFORE, "custom": "changed"}
        for states in ([{}, response(drift)], [{}, response(BEFORE), response(drift)]):
            with self.assertRaisesRegex(
                SystemExit, "precondition|changed before write"
            ):
                self.run_cli(self.apply_args(), states)
            self.assertFalse(self.snapshots.exists())
            self.assertFalse(
                any(c.args[0] == "PUT" for c in self.http.call.call_args_list)
            )
            self.http.reset_mock()

    def test_only_boolean_changes_and_selected_settings_reach_snapshot(self):
        environment = dict(os.environ)
        self.run_cli(
            self.apply_args(),
            [{}, response(BEFORE), response(BEFORE), {}, response(self.desired)],
        )
        puts = [c for c in self.http.call.call_args_list if c.args[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0].args[2], {"config": self.desired})
        self.assertFalse(puts[0].kwargs["retry_429"])
        snapshot = next(self.snapshots.glob("*.json"))
        self.assertEqual(
            json.loads(snapshot.read_text()),
            {
                "target": {"name": "main", "uuid": TARGET[1]},
                "config": BEFORE,
            },
        )
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.snapshots.stat().st_mode & 0o777, 0o700)
        self.settings.get.assert_any_call("MDBLIST_API_KEY")
        self.assertEqual(os.environ, environment)
        self.assertTrue(BEFORE["jellyfin"]["resolveOnOpen"])

    def test_idempotency_makes_no_put_or_snapshot(self):
        self.run_cli(
            self.apply_args(self.desired),
            [{}, response(self.desired), response(self.desired)],
        )
        self.assertFalse(self.snapshots.exists())
        self.assertFalse(any(c.args[0] == "PUT" for c in self.http.call.call_args_list))

    def test_lock_contention_refuses_before_login(self):
        with (
            browse.writer_lock(self.root),
            self.assertRaisesRegex(SystemExit, "lock is busy"),
        ):
            self.run_cli(self.apply_args())
        self.http.call.assert_not_called()

    def test_boolean_restoration(self):
        self.run_cli(
            [*self.apply_args(self.desired), "--resolve-on-open", "true"],
            [
                {},
                response(self.desired),
                response(self.desired),
                {},
                response(BEFORE),
            ],
        )
        put = next(c for c in self.http.call.call_args_list if c.args[0] == "PUT")
        self.assertEqual(put.args[2], {"config": BEFORE})

    def test_bootstrap_preserves_boolean_and_new_default(self):
        for value in (True, False):
            self.assertIs(
                bootstrap.desired_jellyfin(HOUSEHOLD, {"resolveOnOpen": value})[
                    "resolveOnOpen"
                ],
                value,
            )
        for config in ({}, {"resolveOnOpen": "false"}):
            self.assertIs(
                bootstrap.desired_jellyfin(HOUSEHOLD, config)["resolveOnOpen"], True
            )

    def test_429_and_transport_ambiguity_reconcile_once(self):
        for status in (429, None):
            for committed in (True, False):
                self.http.reset_mock()
                states = [
                    {},
                    response(BEFORE),
                    response(BEFORE),
                    common.RequestError("PUT", status, True),
                    response(self.desired if committed else BEFORE),
                ]
                if committed:
                    self.run_cli(self.apply_args(), states)
                else:
                    with self.assertRaisesRegex(SystemExit, "no change was made"):
                        self.run_cli(self.apply_args(), states)
                puts = [c for c in self.http.call.call_args_list if c.args[0] == "PUT"]
                self.assertEqual(len(puts), 1)
                self.assertFalse(puts[0].kwargs["retry_429"])

    def test_rollback_ambiguity_requires_full_restoration(self):
        wrong = copy.deepcopy(BEFORE)
        wrong["jellyfin"]["personas"] = []
        for rollback_status in (None, 429):
            for restored in (True, False):
                self.http.reset_mock()
                states = [
                    response(self.desired),
                    common.RequestError("PUT", rollback_status, True),
                    response(BEFORE if restored else wrong),
                ]
                with self.assertRaisesRegex(
                    SystemExit, "rollback verified" if restored else "rollback failed"
                ):
                    self.http.call.side_effect = states
                    bootstrap.rollback_config(
                        self.http,
                        {},
                        BEFORE,
                        TARGET,
                        self.snapshots / "retained.json",
                        "verification failed",
                        expected=self.desired,
                    )
                puts = [c for c in self.http.call.call_args_list if c.args[0] == "PUT"]
                self.assertEqual(len(puts), 1)
                self.assertTrue(all(c.kwargs["retry_429"] is False for c in puts))

    def test_actual_http_429_put_is_single_attempt(self):
        http = common.Http("http://example.invalid")
        error = urllib.error.HTTPError("http://example.invalid", 429, "fake", {}, None)
        with (
            patch.object(http.opener, "open", side_effect=error) as opener,
            self.assertRaises(common.RequestError),
        ):
            http.call("PUT", "/api/v1/user", {"config": self.desired}, retry_429=False)
        self.assertEqual(opener.call_count, 1)


class RehearsalTests(unittest.TestCase):
    def test_staged_cli_and_deliberate_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments, expected in (([], 0), (["--deliberate-failure"], 1)):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "tests/rehearse_browse_tuning.py"),
                        "--scratch-root",
                        directory,
                        *arguments,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, expected, result.stdout + result.stderr
                )
                if expected:
                    self.assertIn("intentional assertion failure", result.stderr)
                else:
                    self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
