import contextlib
import copy
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import bootstrap
import common
from test_bootstrap_rows import HOUSEHOLD, checker, preset, snapshot

ROOT = Path(__file__).resolve().parent.parent
TARGET = ("person", "private-uuid", "private-password", False)


def response(config):
    return {"data": {"userData": config}}


class ConfigLoaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "configs.json"
        self.entry = {
            "name": "person",
            "uuid": "private-uuid",
            "password": "private-password",
        }

    def write(self, body):
        self.path.write_text(json.dumps(body))
        self.path.chmod(0o600)

    def test_valid_order_and_missing_file(self):
        self.assertEqual(common.load_configs(self.root, "main-id"), [])
        entries = [self.entry, {**self.entry, "name": "other", "uuid": "other-id"}]
        self.write({"configs": entries})
        self.assertEqual(common.load_configs(self.root, "main-id"), entries)

    def test_exact_schema_strings_names_duplicates_and_main_uuid(self):
        cases = [
            [],
            {},
            {"configs": [], "extra": 1},
            {"configs": {}},
            {"configs": [None]},
            {"configs": [{**self.entry, "extra": 1}]},
        ]
        for field in self.entry:
            for value in (None, "", " ", 4):
                cases.append({"configs": [{**self.entry, field: value}]})
            cases.append(
                {
                    "configs": [
                        {
                            key: value
                            for key, value in self.entry.items()
                            if key != field
                        }
                    ]
                }
            )
        for name in (
            "main",
            "UPPER",
            "a-b",
            "a" * 33,
            "https://secret.example",
            "x\ny",
        ):
            cases.append({"configs": [{**self.entry, "name": name}]})
        cases.extend(
            [
                {"configs": [self.entry, self.entry]},
                {"configs": [self.entry, {**self.entry, "uuid": "other"}]},
                {"configs": [self.entry, {**self.entry, "name": "other"}]},
                {"configs": [{**self.entry, "uuid": "main-id"}]},
            ]
        )
        for body in cases:
            with self.subTest(body=body):
                self.write(body)
                with self.assertRaises(SystemExit) as caught:
                    common.load_configs(self.root, "main-id")
                self.assertNotIn("private-uuid", str(caught.exception))
                self.assertNotIn("private-password", str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))

    def test_permissions_owner_symlink_and_directory(self):
        self.write({"configs": [self.entry]})
        for mode in (0o640, 0o604, 0o610, 0o601):
            self.path.chmod(mode)
            with self.assertRaises(SystemExit):
                common.load_configs(self.root, "main-id")
        self.path.chmod(0o600)
        with (
            patch.object(common.os, "geteuid", return_value=os.geteuid() + 1),
            self.assertRaises(SystemExit),
        ):
            common.load_configs(self.root, "main-id")
        other = self.root / "other"
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises(SystemExit):
            common.load_configs(self.root, "main-id")
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaises(SystemExit):
            common.load_configs(self.root, "main-id")

    def test_malformed_json_is_quiet(self):
        self.path.write_text("private-password")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(SystemExit, "cannot be read as JSON"):
            common.load_configs(self.root, "main-id")

    def test_main_and_standalone_resolution_and_known_names(self):
        self.write({"configs": [self.entry]})
        settings = common.Settings(self.root)
        with patch.dict(
            os.environ,
            {"AIOSTREAMS_UUID": "main-id", "AIOSTREAMS_PASSWORD": "main-password"},
        ):
            for name in (None, "main"):
                self.assertEqual(
                    common.resolve_target(settings, name),
                    ("main", "main-id", "main-password", True),
                )
            self.assertEqual(common.resolve_target(settings, "person"), TARGET)
            with self.assertRaisesRegex(SystemExit, "known names: main, person"):
                common.resolve_target(settings, "private-password")


class SyncStateTests(unittest.TestCase):
    def setUp(self):
        self.current = {
            "presets": [{**preset("a"), "credentials": {"token": "private-password"}}],
            "services": [
                {"id": "torbox", "credentials": {"apiKey": "private-password"}}
            ],
            "jellyfin": {"personas": [], "nested": {"pin": "private-password"}},
            "sortCriteria": [{"key": "old"}],
            "other": "keep",
        }
        self.template = {
            "presets": [preset("a"), preset("new")],
            "services": [
                {"id": "torbox", "credentials": {"apiKey": "template-secret"}}
            ],
            "sortCriteria": [{"key": "new"}],
            "catalogModifications": [],
        }

    def test_sync_replaces_keys_carries_credentials_and_keeps_unmanaged(self):
        original = copy.deepcopy(self.current)
        desired = bootstrap.sync_template(self.current, self.template)
        self.assertEqual(desired["sortCriteria"], self.template["sortCriteria"])
        self.assertEqual(desired["services"], self.current["services"])
        self.assertEqual(
            desired["presets"][0]["credentials"],
            self.current["presets"][0]["credentials"],
        )
        self.assertEqual(desired["other"], "keep")
        self.assertEqual(self.current, original)
        self.assertEqual([p["instanceId"] for p in desired["presets"]], ["a", "new"])

    def test_duplicate_ids_and_removal_guard(self):
        for current, template in (
            ({"presets": [preset("a"), preset("a")]}, {}),
            ({}, {"presets": [preset("a"), preset("a")]}),
        ):
            with self.assertRaisesRegex(SystemExit, "duplicate"):
                bootstrap.sync_template(current, template, True)
        with self.assertRaisesRegex(SystemExit, "--allow-preset-removal"):
            bootstrap.sync_template(self.current, {"presets": []})
        self.assertEqual(
            bootstrap.sync_template(self.current, {"presets": []}, True)["presets"], []
        )

    def test_credential_absence_and_null_preserved(self):
        for credentials in ({}, {"credentials": None}, {"credentials": {}}):
            current = {"presets": [{**preset("a"), **credentials}]}
            desired = bootstrap.sync_template(
                current, {"presets": [{**preset("a"), "credentials": {"token": "new"}}]}
            )
            self.assertEqual(desired["presets"], current["presets"])
            bootstrap.verify_config(desired, desired, current, False)

    def test_path_specific_hash_includes_key_and_nested_credentials(self):
        digest = bootstrap.sync_sha256(self.current, self.template)
        for field in ("presets", "services"):
            changed = copy.deepcopy(self.current)
            changed[field][0]["credentials"] = {"secret": "different"}
            self.assertEqual(bootstrap.sync_sha256(changed, self.template), digest)
        changed = copy.deepcopy(self.current)
        changed["sortCriteria"][0]["key"] = "different"
        self.assertNotEqual(bootstrap.sync_sha256(changed, self.template), digest)
        for field in ("key", "token", "credentials", "password"):
            changed = copy.deepcopy(self.current)
            changed["presets"][0]["options"][field] = "different"
            self.assertNotEqual(bootstrap.sync_sha256(changed, self.template), digest)
            self.assertNotEqual(
                bootstrap.rows_sha256(changed), bootstrap.rows_sha256(self.current)
            )
        self.assertEqual(
            len(
                {
                    bootstrap.sync_sha256(config, {"sortCriteria": []})
                    for config in ({}, {"sortCriteria": None}, {"sortCriteria": []})
                }
            ),
            3,
        )

    def test_full_comparison_ignores_only_server_maintained_top_level_fields(self):
        self.assertEqual(
            common.SERVER_MAINTAINED_KEYS,
            (
                "statistics",
                "trusted",
                "ip",
                "activeVariants",
                "autoVariants",
                "healthResults",
                "variantSelectorLocation",
            ),
        )
        for key in common.SERVER_MAINTAINED_KEYS:
            observed = {**self.current, key: "server-rewrites-on-save"}
            bootstrap.verify_config(observed, self.current, self.current, False)
        for key in ("unmanaged", "password", "key", "history"):
            with self.assertRaisesRegex(SystemExit, "verification failed"):
                bootstrap.verify_config(
                    {**self.current, key: "unexpected"},
                    self.current,
                    self.current,
                    False,
                )
        for key in common.SERVER_MAINTAINED_KEYS:
            self.assertNotEqual(
                common.canonical_config({"nested": {key: 1}}),
                common.canonical_config({"nested": {key: 2}}),
            )

    def test_credential_and_jellyfin_invariants_are_explicit(self):
        changed = copy.deepcopy(self.current)
        changed["presets"][0]["credentials"] = {"token": "other"}
        with self.assertRaisesRegex(SystemExit, "credential"):
            bootstrap.verify_config(changed, changed, self.current, False)
        changed = {**self.current, "jellyfin": {}}
        with self.assertRaisesRegex(SystemExit, "Jellyfin"):
            bootstrap.verify_config(changed, changed, self.current, False)

    def test_standalone_jellyfin_preserved_in_every_mode_including_absence(self):
        for jellyfin in (
            {},
            {"jellyfin": None},
            {"jellyfin": self.current["jellyfin"]},
        ):
            current = {"presets": [], **jellyfin}
            template = {"presets": [preset("new")], "jellyfin": {"wrong": True}}
            for rows, sync, apply in (
                (False, False, False),
                (True, False, False),
                (False, True, False),
                (False, False, True),
            ):
                with self.subTest(jellyfin=jellyfin, rows=rows, sync=sync, apply=apply):
                    http = Mock()
                    with (
                        tempfile.TemporaryDirectory() as directory,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        digest = (
                            bootstrap.sync_sha256(current, template)
                            if sync
                            else bootstrap.rows_sha256(current)
                        )
                        desired = (
                            bootstrap.sync_template(current, template)
                            if sync
                            else bootstrap.apply_rows(current, template)
                            if rows
                            else bootstrap.apply_template(current, template)
                            if apply
                            else copy.deepcopy(current)
                        )
                        if "jellyfin" in current:
                            desired["jellyfin"] = current["jellyfin"]
                        else:
                            desired.pop("jellyfin", None)
                        http.call.side_effect = [{}, response(desired)]
                        bootstrap.converge_config(
                            http,
                            {},
                            {},
                            template,
                            apply,
                            common.Plan(False),
                            response(current),
                            rows,
                            False,
                            digest,
                            sync,
                            False,
                            TARGET,
                            directory,
                        )
                    if http.call.called:
                        written = http.call.call_args_list[0].args[2]["config"]
                        self.assertEqual("jellyfin" in written, "jellyfin" in current)
                        self.assertEqual(
                            written.get("jellyfin"), current.get("jellyfin")
                        )
                    else:
                        self.assertFalse(rows or sync or apply)

    def test_sync_diff_lists_paths_and_tuples_without_values_or_credentials(self):
        desired = bootstrap.sync_template(self.current, self.template)
        desired["presets"][0]["options"]["url"] = (
            "https://private.example/private-password"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bootstrap.print_sync_diff(self.current, desired, self.template)
        result = output.getvalue()
        for secret in (
            "private-password",
            "private-uuid",
            "https://",
            "template-secret",
        ):
            self.assertNotIn(secret, result)
        self.assertIn("sortCriteria: changed", result)
        self.assertIn("presets added: ['new']", result)
        self.assertIn("presets removed: []", result)
        self.assertIn("presets[a].options.url", result)
        self.assertIn("expect_sha256=", result)


class GuardedWriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.before = {"presets": [preset("a")], "jellyfin": {}, "unmanaged": 1}
        self.desired = {**self.before, "sortCriteria": [{"key": "new"}]}
        self.http = Mock()
        self.output = io.StringIO()

    def write(self):
        with contextlib.redirect_stdout(self.output):
            return bootstrap.guarded_write(
                self.http, {}, self.before, self.desired, TARGET, self.directory.name
            )

    def test_snapshot_precedes_put_and_success(self):
        def put(method, *args, **kwargs):
            if method == "PUT":
                files = list(Path(self.directory.name).glob("*.json"))
                self.assertEqual(len(files), 1)
                body = json.loads(files[0].read_text())
                self.assertEqual(
                    body,
                    {
                        "target": {"name": TARGET[0], "uuid": TARGET[1]},
                        "config": self.before,
                    },
                )
                return {}
            return response(self.desired)

        self.http.call.side_effect = put
        self.assertEqual(self.write(), response(self.desired))
        path = Path(self.output.getvalue().strip())
        self.assertRegex(path.name, r"^person-\d{8}T\d{6}Z-[0-9a-f]{8}\.json$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_post_write_mismatch_rolls_back_and_verifies(self):
        self.http.call.side_effect = [
            {},
            response({**self.desired, "unmanaged": 2}),
            {},
            response(self.before),
        ]
        with self.assertRaisesRegex(
            SystemExit, "verification failed; rollback verified"
        ):
            self.write()
        self.assertEqual(
            [call.args[0] for call in self.http.call.call_args_list],
            ["PUT", "GET", "PUT", "GET"],
        )
        self.assertEqual(
            self.http.call.call_args_list[2].args[2], {"config": self.before}
        )

    def test_ambiguous_put_desired_prewrite_and_neither(self):
        for observed, suffix, rollback in (
            (self.desired, None, False),
            (self.before, "no change was made", False),
            ({"unexpected": 1}, "rollback verified", True),
        ):
            with self.subTest(observed=observed):
                self.http.reset_mock()
                self.http.call.side_effect = [
                    common.RequestError("PUT", None, True),
                    response(observed),
                ] + ([{}, response(self.before)] if rollback else [])
                if suffix:
                    with self.assertRaisesRegex(SystemExit, suffix):
                        self.write()
                else:
                    self.assertEqual(self.write(), response(self.desired))
                self.assertEqual(self.http.call.call_count, 4 if rollback else 2)

    def test_reconciliation_get_failure_rolls_back_after_all_retries(self):
        for ambiguous in (True, False):
            self.http.call.side_effect = (
                ([common.RequestError("PUT", None, True)] if ambiguous else [{}])
                + [common.RequestError("GET", 503)] * 4
                + [{}, response(self.before)]
            )
            with (
                patch.object(common.time, "sleep") as sleep,
                self.assertRaisesRegex(SystemExit, "rollback verified"),
            ):
                self.write()
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list], [5, 15, 30]
            )
            self.assertEqual(
                self.http.call.call_args_list[-2].args[2], {"config": self.before}
            )

    def test_rollback_put_or_verification_failure_reports_both_and_exact_restore(self):
        for rollback in (
            [common.RequestError("PUT", 400, True)],
            [{}, response({"wrong": True})],
            [{}, common.RequestError("GET", 403)],
        ):
            self.http.call.side_effect = [{}, response({"wrong": True}), *rollback]
            with self.assertRaises(SystemExit) as caught:
                self.write()
            message = str(caught.exception)
            self.assertIn(
                "full configuration verification failed; rollback failed", message
            )
            match = re.search(r"emergency snapshot: ([^\n]+)", message)
            self.assertIsNotNone(match)
            self.assertIn(
                f"python3 ops/config-snapshot.py restore --config person {match[1]}",
                message,
            )
            self.assertNotIn("private-password", message)
            self.assertNotIn("private-uuid", message)
            self.assertNotIn("https://", message)

    def test_programming_errors_and_interrupts_propagate_without_reconciliation(self):
        for error in (TypeError("bug"), KeyboardInterrupt()):
            self.http.reset_mock()
            self.http.call.side_effect = error
            with self.assertRaises(type(error)):
                self.write()
            self.assertEqual(self.http.call.call_count, 1)

    def test_hash_required_or_mismatch_prevents_snapshot_and_http(self):
        for rows, sync in ((True, False), (False, True)):
            for digest in (None, "wrong"):
                with (
                    self.assertRaisesRegex(SystemExit, "--expect-sha256"),
                    patch.object(bootstrap, "save_snapshot") as save,
                ):
                    bootstrap.converge_config(
                        self.http,
                        {},
                        {},
                        {},
                        False,
                        common.Plan(False),
                        response(self.before),
                        rows,
                        False,
                        digest,
                        sync,
                        False,
                        TARGET,
                    )
                save.assert_not_called()
                self.http.call.assert_not_called()


class SnapshotSafetyTests(unittest.TestCase):
    def test_target_match_name_uuid_and_legacy_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "save.json"
            common.save_snapshot(path, {"value": 1}, TARGET)
            self.assertEqual(
                snapshot.load_snapshot(path, TARGET)["config"], {"value": 1}
            )
            for target in (("main", TARGET[1]), (TARGET[0], "different")):
                with self.assertRaisesRegex(SystemExit, "target must match"):
                    snapshot.load_snapshot(path, target)
            for body in (
                {"config": {}},
                {
                    "target": {"name": "person", "uuid": TARGET[1]},
                    "config": [],
                    "extra": 1,
                },
            ):
                path.write_text(json.dumps(body))
                for target in (TARGET, ("main", TARGET[1])):
                    with self.assertRaisesRegex(
                        SystemExit, "legacy snapshots are rejected"
                    ):
                        snapshot.load_snapshot(path, target)

    def test_directory_modes_owner_symlink_and_non_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshots"
            self.assertEqual(common.private_directory(path), path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for mode in (0o750, 0o701, 0o770):
                path.chmod(mode)
                with self.assertRaises(SystemExit):
                    common.private_directory(path)
            path.chmod(0o700)
            with (
                patch.object(common.os, "geteuid", return_value=os.geteuid() + 1),
                self.assertRaises(SystemExit),
            ):
                common.private_directory(path)
            link = Path(directory) / "link"
            link.symlink_to(path, target_is_directory=True)
            with self.assertRaises(SystemExit):
                common.private_directory(link)
            link.unlink()
            link.write_text("file")
            with self.assertRaises(SystemExit):
                common.private_directory(link)

    def test_generated_names_are_distinct_and_restore_uses_only_config(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {common.snapshot_path(directory, "person") for _ in range(10)}
            self.assertEqual(len(paths), 10)
        http = Mock()
        body = {
            "target": {"name": TARGET[0], "uuid": TARGET[1]},
            "config": {"key": "value"},
        }
        http.call.side_effect = [{}, response(body["config"])]
        snapshot.restore_snapshot(http, {}, body)
        self.assertEqual(
            http.call.call_args_list[0].args[2], {"config": body["config"]}
        )


class HttpRetryTests(unittest.TestCase):
    def test_retry_after_integer_date_clamps_malformed_and_past(self):
        now = datetime(2026, 10, 12, tzinfo=UTC)
        cases = [
            (None, 60),
            ("", 60),
            ("broken", 60),
            ("-1", 60),
            ("1.5", 60),
            ("0", 1),
            ("12", 12),
            ("999", 330),
            (format_datetime(now + timedelta(seconds=20)), 20),
            (format_datetime(now + timedelta(seconds=900)), 330),
            (format_datetime(now - timedelta(seconds=1)), 60),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(common.retry_after(value, now), expected)

    def test_429_retries_three_times_jitter_and_safe_output(self):
        http = common.Http("https://private.example")
        error = urllib.error.HTTPError(
            "https://private.example/uuid/password",
            429,
            "private-password",
            {"Retry-After": "2"},
            None,
        )
        output = io.StringIO()
        with (
            patch.object(http.opener, "open", side_effect=error) as opener,
            patch.object(common.time, "sleep") as sleep,
            patch.object(common.random, "uniform", return_value=3),
            contextlib.redirect_stdout(output),
            self.assertRaises(common.RequestError) as caught,
        ):
            http.call("PUT", "/uuid/private-password", {"password": "private-password"})
        self.assertEqual(opener.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5, 5])
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(caught.exception.committed_unknown)
        self.assertEqual(output.getvalue().count("retry"), 3)
        for value in ("https://", "uuid", "private-password"):
            self.assertNotIn(value, str(caught.exception) + output.getvalue())

    def test_transport_and_other_http_failures_are_typed(self):
        for method in ("GET", "PUT"):
            for status in (None, 400, 503):
                http = common.QuietHttp("https://private.example")
                error = (
                    urllib.error.URLError("private-password")
                    if status is None
                    else urllib.error.HTTPError(
                        "https://private.example", status, "private-password", {}, None
                    )
                )
                with (
                    patch.object(http.opener, "open", side_effect=error) as opener,
                    patch.object(common.time, "sleep"),
                    self.assertRaises(common.RequestError) as caught,
                ):
                    http.call(method, "/private-uuid")
                self.assertEqual(caught.exception.method, method)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.committed_unknown, method == "PUT")
                self.assertEqual(
                    opener.call_count, 4 if status == 503 and method == "GET" else 1
                )

    def test_5xx_retries_only_safe_methods(self):
        for method, expected_calls in (("PUT", 1), ("POST", 1), ("GET", 4)):
            with self.subTest(method=method):
                http = common.Http("https://private.example")
                error = urllib.error.HTTPError(
                    "https://private.example/user", 503, "unavailable", {}, None
                )
                with (
                    patch.object(http.opener, "open", side_effect=error) as opener,
                    patch.object(common.time, "sleep"),
                    self.assertRaises(common.RequestError) as caught,
                ):
                    http.call(method, "/api/v1/user", {"config": {}})
                self.assertEqual(opener.call_count, expected_calls)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(caught.exception.committed_unknown, method == "PUT")

    def test_reconciliation_retries_transport_5xx_429_only(self):
        for status in (None, 429, 500, 599, 403):
            http = Mock()
            http.call.side_effect = common.RequestError("GET", status)
            with (
                patch.object(common.time, "sleep") as sleep,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(common.RequestError),
            ):
                common.reconciliation_get(http, {})
            self.assertEqual(http.call.call_count, 1 if status == 403 else 4)
            self.assertEqual(sleep.call_count, 0 if status == 403 else 3)
            for call in http.call.call_args_list:
                self.assertFalse(call.kwargs["retry_429"])


class CliTests(unittest.TestCase):
    def test_main_sync_converges_household_personas(self):
        current = {"presets": []}
        template = {"presets": [preset("new")]}
        desired = bootstrap.sync_template(current, template)
        desired["jellyfin"] = bootstrap.desired_jellyfin(HOUSEHOLD, {})
        http = Mock()
        http.call.side_effect = [{}, response(desired)]
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = bootstrap.converge_config(
                http,
                {},
                HOUSEHOLD,
                template,
                False,
                common.Plan(False),
                response(current),
                expect_sha256=bootstrap.sync_sha256(current, template),
                sync_template_keys=True,
                target=("main", "main-id", "main-password", True),
                snapshot_directory=directory,
            )
        self.assertEqual(result, response(desired))
        self.assertEqual(
            http.call.call_args_list[0].args[2]["config"]["jellyfin"],
            desired["jellyfin"],
        )

    def test_save_cli_existing_path_reports_one_line_without_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.json"
            path.write_text("existing")
            with (
                patch.object(sys, "argv", ["config-snapshot.py", "save", str(path)]),
                patch.object(snapshot, "Settings") as settings,
                patch.object(snapshot, "QuietHttp") as http,
                patch.object(snapshot, "aiostreams_session") as authenticate,
                self.assertRaises(SystemExit) as caught,
            ):
                snapshot.main()
            self.assertEqual(str(caught.exception), f"snapshot already exists: {path}")
            settings.assert_not_called()
            http.assert_not_called()
            authenticate.assert_not_called()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "ops" / "config-snapshot.py"),
                    "save",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, f"snapshot already exists: {path}\n")
            self.assertEqual(result.stdout, "")
            self.assertEqual(path.read_text(), "existing")

    def test_snapshot_save_and_restore_cli_use_target_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    ["config-snapshot.py", "save", "--config", "person", directory],
                ),
                patch.object(snapshot, "resolve_target", return_value=TARGET),
                patch.object(snapshot, "Settings") as settings,
                patch.object(snapshot, "QuietHttp") as constructor,
                contextlib.redirect_stdout(output),
            ):
                settings.return_value.get.return_value = "admin:secret"
                constructor.return_value.call.side_effect = [
                    {},
                    response({"key": "saved"}),
                ]
                snapshot.main()
            path = Path(output.getvalue().strip())
            self.assertEqual(
                json.loads(path.read_text()),
                {
                    "target": {"name": TARGET[0], "uuid": TARGET[1]},
                    "config": {"key": "saved"},
                },
            )
            self.assertEqual(
                constructor.return_value.call.call_args_list[1].kwargs["headers"],
                common.basic(TARGET[1], TARGET[2]),
            )
            output = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    ["config-snapshot.py", "restore", "--config", "person", str(path)],
                ),
                patch.object(snapshot, "resolve_target", return_value=TARGET),
                patch.object(snapshot, "Settings") as settings,
                patch.object(snapshot, "QuietHttp") as constructor,
                contextlib.redirect_stdout(output),
            ):
                settings.return_value.get.return_value = "admin:secret"
                constructor.return_value.call.side_effect = [
                    {},
                    {},
                    response({"key": "saved", "healthResults": []}),
                ]
                snapshot.main()
            self.assertEqual(output.getvalue(), "restored and verified config person\n")
            with (
                patch.object(
                    sys,
                    "argv",
                    ["config-snapshot.py", "restore", "--config", "main", str(path)],
                ),
                patch.object(
                    snapshot,
                    "resolve_target",
                    return_value=("main", TARGET[1], TARGET[2], True),
                ),
                patch.object(snapshot, "QuietHttp") as constructor,
                self.assertRaisesRegex(SystemExit, "target must match"),
            ):
                snapshot.main()
            constructor.assert_not_called()

    def test_standalone_urls_and_sync_creation_rejected_before_http(self):
        for argv in (
            ["--config", "person", "--print-urls"],
            ["--sync-template"],
            ["--sync-template", "--apply-rows"],
            ["--allow-preset-removal"],
        ):
            with (
                patch.object(sys, "argv", ["bootstrap.py", *argv]),
                patch.object(bootstrap, "QuietHttp") as http,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                bootstrap.main()
            http.assert_not_called()
        with (
            patch.object(sys, "argv", ["bootstrap.py", "--sync-template", "--dry-run"]),
            patch.object(
                bootstrap, "resolve_target", return_value=("main", "", "", True)
            ),
            patch.object(bootstrap, "QuietHttp") as http,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            bootstrap.main()
        http.assert_not_called()

    def test_standalone_sync_cli_uses_resolved_target_without_household(self):
        template = json.loads(
            (ROOT / "config" / "aiostreams.template.json").read_text()
        )
        current = copy.deepcopy(template)
        current["jellyfin"] = {"keep": True}
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "bootstrap.py",
                    "--config",
                    "person",
                    "--sync-template",
                    "--dry-run",
                    "--diff",
                ],
            ),
            patch.object(bootstrap, "resolve_target", return_value=TARGET),
            patch.object(bootstrap, "Settings") as settings,
            patch.object(bootstrap, "QuietHttp") as constructor,
            contextlib.redirect_stdout(output),
        ):
            settings.return_value.get.side_effect = lambda key, required=False: {
                "AIOSTREAMS_AUTH": "admin:secret",
                "AIOSTREAMS_PUBLIC_URL": "https://private.example",
                "MDBLIST_API_KEY": "test-mdblist-key",
                "MDBLIST_ROWS": "on",
            }[key]
            constructor.return_value.call.side_effect = [{}, response(current)]
            bootstrap.main()
        self.assertEqual(
            constructor.return_value.call.call_args_list[1].kwargs["headers"],
            common.basic(TARGET[1], TARGET[2]),
        )
        for value in (TARGET[1], TARGET[2], "https://"):
            self.assertNotIn(value, output.getvalue())

    def test_check_catalogs_uses_standalone_uuid_and_password(self):
        fixture = json.loads((ROOT / "config" / "expected-catalogs.json").read_text())
        with (
            patch.object(sys, "argv", ["check-catalogs.py", "--config", "person"]),
            patch.object(checker, "resolve_target", return_value=TARGET),
            patch.object(checker, "Settings") as settings,
            patch.object(checker, "QuietHttp") as constructor,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            settings.return_value.get.return_value = "admin:secret"
            http = constructor.return_value
            http.call.side_effect = [
                {},
                {"data": {"encryptedPassword": "private-encrypted"}},
                {"catalogs": fixture["manifest"]},
                {"User": {"Id": "id"}, "AccessToken": "token"},
                {"Items": [{"Name": name} for name in fixture["views"]]},
            ]
            checker.main()
        self.assertEqual(
            http.call.call_args_list[3].args[2],
            {"Username": TARGET[1], "Pw": TARGET[2]},
        )


class EachConfigTests(unittest.TestCase):
    def run_fake(self, pause=None, fail="", arguments=None, inventory_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ops").mkdir()
            (root / "ops" / "each-config.sh").write_text(
                (ROOT / "ops" / "each-config.sh").read_text()
            )
            (root / "ops" / "bootstrap.py").write_text("")
            bin_path = root / "bin"
            bin_path.mkdir()
            python = bin_path / "python3"
            python.write_text("""#!/usr/bin/env bash
set -eu
if [ "$#" -eq 1 ] && [ "$1" = - ]; then
  cat >/dev/null
  if [ "$INVENTORY_FAILURE" = 1 ]; then exit 1; fi
  printf 'main\\nperson\\nother\\n'
else
  printf '%s\\n' "$*" >> "$LOG"
  if [[ "$*" = *"--config $FAIL" ]] && [ -n "$FAIL" ]; then exit 1; fi
fi
""")
            python.chmod(0o700)
            sleep = bin_path / "sleep"
            sleep.write_text(
                '#!/usr/bin/env bash\nprintf "sleep %s\\n" "$1" >> "$LOG"\n'
            )
            sleep.chmod(0o700)
            log = root / "log"
            env = {
                **os.environ,
                "PATH": f"{bin_path}:{os.environ['PATH']}",
                "LOG": str(log),
                "FAIL": fail,
                "INVENTORY_FAILURE": "1" if inventory_failure else "0",
            }
            env.pop("EACH_CONFIG_PAUSE", None)
            if pause is not None:
                env["EACH_CONFIG_PAUSE"] = pause
            result = subprocess.run(
                [
                    "bash",
                    str(root / "ops" / "each-config.sh"),
                    *(arguments or ["ops/bootstrap.py", "--dry-run"]),
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return result, log.read_text().splitlines() if log.exists() else []

    def test_main_first_order_default_pause_and_stop_on_failure(self):
        result, lines = self.run_fake(fail="person")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            lines,
            [
                "ops/bootstrap.py --dry-run --config main",
                "sleep 40",
                "ops/bootstrap.py --dry-run --config person",
            ],
        )
        self.assertEqual(result.stderr, "config person failed; stopped\n")
        result, lines = self.run_fake(pause="30")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines[-1], "ops/bootstrap.py --dry-run --config other")
        self.assertEqual(lines.count("sleep 30"), 2)

    def test_pause_validation_and_inventory_failure(self):
        for value in ("29", "601", "-1", "30.0", "no", "999999999999999999999", ""):
            with self.subTest(value=value):
                result, lines = self.run_fake(pause=value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(lines, [])
        result, lines = self.run_fake(inventory_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lines, [])
        result, lines = self.run_fake(
            arguments=["ops/bootstrap.py", "--config", "person"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
