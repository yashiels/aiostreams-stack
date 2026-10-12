import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops"))
import bootstrap
from common import Plan, QuietHttp, RequestError


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "ops" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load_script("config-snapshot")
checker = load_script("check-catalogs")


def preset(identifier):
    return {
        "instanceId": identifier,
        "enabled": True,
        "type": "addon",
        "options": {"name": identifier},
    }


HOUSEHOLD = {
    "users": [{"slug": "admin", "name": "Admin", "color": "000000", "admin": True}]
}


class MergeTests(unittest.TestCase):
    def test_insert_at_template_positions(self):
        template = [preset(name) for name in ("a", "b", "c", "d", "e")]
        merged = bootstrap.merge_presets([preset("b"), preset("d")], template)
        self.assertEqual([p["instanceId"] for p in merged], ["a", "b", "c", "d", "e"])
        self.assertEqual(bootstrap.merge_presets([], template), template)

    def test_existing_presets_and_other_keys_untouched(self):
        existing = preset("a")
        existing["credentials"] = {"apiKey": "private"}
        existing["options"]["credentials"] = {"password": "private"}
        current = {
            "presets": [preset("custom"), existing, preset("z")],
            "services": [{"credentials": {"token": "private"}}],
            "other": {"nested": [1]},
        }
        original = copy.deepcopy(current)
        template = {
            "presets": [preset("a"), preset("b")],
            "catalogModifications": [{"id": "row", "type": "movie", "enabled": True}],
        }
        desired = bootstrap.apply_rows(current, template)
        self.assertEqual(current, original)
        self.assertEqual(
            [p["instanceId"] for p in desired["presets"]], ["custom", "a", "b", "z"]
        )
        self.assertEqual(desired["presets"][1], existing)
        for key in ("services", "other"):
            self.assertEqual(desired[key], current[key])
        desired["presets"][1]["credentials"]["apiKey"] = "changed"
        self.assertEqual(current, original)

    def test_existing_order_is_preserved(self):
        current = [preset("c"), preset("a")]
        merged = bootstrap.merge_presets(
            current, [preset("a"), preset("b"), preset("c")]
        )
        self.assertEqual([p for p in merged if p["instanceId"] != "b"], current)

    def test_conflict_lists_paths_only(self):
        existing = preset("a")
        existing["options"].update(name="https://private.example/secret", timeout=1)
        template = preset("a")
        template["options"]["timeout"] = 2
        with self.assertRaises(SystemExit) as caught:
            bootstrap.merge_presets([existing], [template])
        self.assertEqual(
            str(caught.exception),
            "preset conflicts: presets.0.options.name, presets.0.options.timeout",
        )

    def test_duplicate_instances_in_either_list(self):
        for current, template in (
            ([preset("a"), preset("a")], []),
            ([], [preset("a"), preset("a")]),
        ):
            with (
                self.subTest(current=current),
                self.assertRaisesRegex(SystemExit, "duplicate preset"),
            ):
                bootstrap.merge_presets(current, template)

    def test_duplicate_catalog_identity(self):
        catalog = {"id": "row", "type": "movie"}
        with self.assertRaisesRegex(SystemExit, "duplicate catalog"):
            bootstrap.apply_rows({}, {"catalogModifications": [catalog, catalog]})
        bootstrap.validate_catalog_modifications(
            [catalog, {"id": "row", "type": "series"}]
        )


class HashAndDiffTests(unittest.TestCase):
    def test_hash_tracks_nested_secret_named_keys(self):
        before = {"presets": [{"instanceId": "a", "options": {"apiKey": "one"}}]}
        after = copy.deepcopy(before)
        after["presets"][0]["options"]["apiKey"] = "two"
        self.assertNotEqual(bootstrap.rows_sha256(before), bootstrap.rows_sha256(after))

    def test_hash_tracks_list_order(self):
        self.assertNotEqual(
            bootstrap.rows_sha256({"presets": [preset("a"), preset("b")]}),
            bootstrap.rows_sha256({"presets": [preset("b"), preset("a")]}),
        )

    def test_diff_never_emits_urls_or_option_values(self):
        current = {"presets": [preset("a")], "catalogModifications": []}
        desired = copy.deepcopy(current)
        desired["presets"][0]["options"].update(
            url="https://private.example/token", arbitrary="private-option"
        )
        desired["catalogModifications"] = [
            {
                "id": "row",
                "type": "movie",
                "name": "https://private.example/token",
                "enabled": True,
            }
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bootstrap.print_rows_diff(current, desired)
        self.assertNotIn("https://", output.getvalue())
        self.assertNotIn("private-option", output.getvalue())
        self.assertIn("changed: presets[a].options.url", output.getvalue())
        self.assertIn("expect_sha256=", output.getvalue())

    def test_http_failure_hides_path_and_response(self):
        http = QuietHttp("https://example.com")
        with (
            patch.object(
                http.opener, "open", side_effect=urllib.error.URLError("private")
            ),
            self.assertRaisesRegex(RequestError, "^GET request failed: no response$"),
        ):
            http.call("GET", "/private", retry_429=False)

    def test_http_failure_preserves_status_only(self):
        for code in (403, 429):
            body = io.BytesIO(b"https://private.example/secret response")
            error = urllib.error.HTTPError(
                "https://private.example/secret", code, "private", {}, body
            )
            http = QuietHttp("https://example.com")
            with (
                self.subTest(code=code),
                patch.object(http.opener, "open", side_effect=error),
                self.assertRaisesRegex(
                    RequestError, f"^POST request failed: HTTP {code}$"
                ),
            ):
                http.call("POST", "/private/path", retry_429=False)
            self.assertTrue(body.closed)

    def test_missing_null_and_empty_row_fields_have_distinct_hashes(self):
        for key in ("presets", "catalogModifications"):
            with self.subTest(key=key):
                configs = [{}, {key: None}, {key: []}]
                self.assertEqual(
                    [bootstrap.row_state(config) for config in configs], configs
                )
                hashes = [bootstrap.rows_sha256(config) for config in configs]
                self.assertEqual(len(set(hashes)), 3)
                for config, digest in zip(configs, hashes, strict=True):
                    canonical = json.dumps(
                        config, sort_keys=True, separators=(",", ":")
                    )
                    self.assertEqual(
                        digest, hashlib.sha256(canonical.encode()).hexdigest()
                    )

    def test_preset_insertions_do_not_report_changes_to_existing_presets(self):
        current = {"presets": [preset("tmd"), preset("stream"), preset("subtitles")]}
        desired = {
            "presets": [
                preset("tmd"),
                preset("stc"),
                preset("tco"),
                preset("stream"),
                preset("subtitles"),
            ]
        }
        self.assertEqual(
            bootstrap.preset_differences(current, desired), (["stc", "tco"], [], [])
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bootstrap.print_rows_diff(current, desired)
        self.assertIn("presets added: ['stc', 'tco']", output.getvalue())
        self.assertIn("presets removed: []", output.getvalue())
        self.assertNotIn("changed:", output.getvalue())

    def test_preset_removal_and_change_use_instance_ids(self):
        current = {"presets": [preset("stc"), preset("removed")]}
        current["presets"][0]["options"]["catalogs"] = ["nfx"]
        desired = {"presets": [preset("added"), copy.deepcopy(current["presets"][0])]}
        desired["presets"][1]["options"]["catalogs"] = ["amp"]
        self.assertEqual(
            bootstrap.preset_differences(current, desired),
            (["added"], ["removed"], ["presets[stc].options.catalogs.0"]),
        )


class ApplyTests(unittest.TestCase):
    def test_missing_hash_exits_before_http(self):
        with (
            patch.object(sys, "argv", ["bootstrap.py", "--apply-rows"]),
            patch.object(bootstrap, "QuietHttp") as http,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            bootstrap.main()
        self.assertNotEqual(caught.exception.code, 0)
        http.assert_not_called()

    def test_diff_requires_apply_rows(self):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["bootstrap.py", "--diff"]),
            patch.object(bootstrap, "Settings") as settings,
            patch.object(bootstrap, "QuietHttp") as http,
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit) as caught,
        ):
            bootstrap.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--diff requires --apply-rows", output.getvalue())
        settings.assert_not_called()
        http.assert_not_called()

    def test_post_write_missing_or_null_fields_fail_verification(self):
        current = {}
        desired = bootstrap.apply_rows(current, {})
        for key in ("presets", "catalogModifications"):
            for missing in (True, False):
                observed = copy.deepcopy(desired)
                if missing:
                    observed.pop(key)
                else:
                    observed[key] = None
                http = Mock()
                http.call.side_effect = [
                    {},
                    {"data": {"userData": observed}},
                    {},
                    {"data": {"userData": current}},
                ]
                with (
                    self.subTest(key=key, missing=missing),
                    self.assertRaisesRegex(SystemExit, "verification failed"),
                ):
                    self.converge(
                        http, current, {}, expected=bootstrap.rows_sha256(current)
                    )

    def test_apply_rows_defaults_missing_and_null_fields(self):
        for current in ({}, {"presets": None, "catalogModifications": None}):
            with self.subTest(current=current):
                self.assertEqual(
                    bootstrap.apply_rows(current, {}),
                    {"presets": [], "catalogModifications": []},
                )

    def test_requires_existing_config(self):
        with (
            patch.object(sys, "argv", ["bootstrap.py", "--apply-rows", "--dry-run"]),
            patch.object(bootstrap, "Settings") as settings,
            patch.object(bootstrap, "QuietHttp") as http,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            settings.return_value.get.return_value = ""
            with self.assertRaises(SystemExit):
                bootstrap.main()
        http.assert_not_called()

    def test_modes_are_mutually_exclusive(self):
        with (
            patch.object(
                sys, "argv", ["bootstrap.py", "--apply-rows", "--apply-template"]
            ),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            bootstrap.main()

    def converge(self, http, current, template, dry_run=False, expected=None):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return bootstrap.converge_config(
                http,
                {},
                HOUSEHOLD,
                template,
                False,
                Plan(dry_run),
                {"data": {"userData": current}},
                True,
                True,
                expected,
                snapshot_directory=directory,
            )

    def test_hash_mismatch_prevents_writes(self):
        http = Mock()
        with self.assertRaisesRegex(SystemExit, "does not match"):
            self.converge(http, {}, {}, expected="wrong")
        http.call.assert_not_called()

    def test_dry_run_does_not_write(self):
        http = Mock()
        self.converge(http, {}, {"presets": [preset("a")]}, dry_run=True)
        http.call.assert_not_called()

    def test_put_then_get_and_persona_convergence(self):
        current = {"presets": [], "unmanaged": "preserved"}
        template = {"presets": [preset("a")], "catalogModifications": []}
        desired = bootstrap.apply_rows(current, template)
        desired["jellyfin"] = bootstrap.desired_jellyfin(HOUSEHOLD, {})
        http = Mock()
        http.call.side_effect = [{}, {"data": {"userData": desired}}]
        self.converge(http, current, template, expected=bootstrap.rows_sha256(current))
        self.assertEqual(
            [call.args[0] for call in http.call.call_args_list], ["PUT", "GET"]
        )
        self.assertEqual(http.call.call_args_list[0].args[2], {"config": desired})

    def test_post_write_mismatch_fails(self):
        http = Mock()
        http.call.side_effect = [
            {},
            {"data": {"userData": {}}},
            {},
            {"data": {"userData": {}}},
        ]
        with self.assertRaisesRegex(SystemExit, "verification failed"):
            self.converge(
                http, {}, {"presets": [preset("a")]}, expected=bootstrap.rows_sha256({})
            )


class SnapshotTests(unittest.TestCase):
    def test_nested_snapshot_parents_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "first" / "second" / "save.json"
            snapshot.save_snapshot(path, {}, ("main", "test-id"))
            for parent in (path.parent, path.parent.parent):
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_save_is_private_exact_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshots" / "save.json"
            snapshot.save_snapshot(
                path, {"nested": {"token": "private"}}, ("main", "test-id")
            )
            self.assertEqual(
                json.loads(path.read_text()),
                {
                    "target": {"name": "main", "uuid": "test-id"},
                    "config": {"nested": {"token": "private"}},
                },
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            with self.assertRaisesRegex(SystemExit, "snapshot already exists"):
                snapshot.save_snapshot(path, {}, ("main", "test-id"))

    def test_restore_ignores_server_fields_only_at_top_level(self):
        body = {"config": {"value": [1], "statistics": {"requests": 1}}}
        http = Mock()
        http.call.side_effect = [
            {},
            {"data": {"userData": {"statistics": {}, "value": [1]}}},
        ]
        snapshot.restore_snapshot(http, {}, body)
        http.call.side_effect = [{}, {"data": {"userData": {"value": [2]}}}]
        with self.assertRaisesRegex(SystemExit, "verification failed"):
            snapshot.restore_snapshot(http, {}, body)
        self.assertNotEqual(
            snapshot.canonical_config({"nested": {"statistics": 1}}),
            snapshot.canonical_config({"nested": {"statistics": 2}}),
        )


class CatalogTests(unittest.TestCase):
    def test_template_fixture_matches_spec(self):
        template = json.loads(
            (ROOT / "config" / "aiostreams.template.json").read_text()
        )
        fixture = json.loads((ROOT / "config" / "expected-catalogs.json").read_text())
        self.assertEqual(
            [p["instanceId"] for p in template["presets"][:4]],
            ["tmd", "stc", "tco", "gac"],
        )
        self.assertEqual(len(fixture["manifest"]), 28)
        self.assertEqual(len(fixture["views"]), 26)
        enabled = {
            (row["type"], row["id"], row["name"])
            for row in template["catalogModifications"]
            if row["enabled"]
        }
        self.assertEqual(
            checker.manifest_entries(fixture["manifest"], "fixture"), enabled
        )
        self.assertEqual(
            fixture["views"],
            [
                row["name"]
                for row in template["catalogModifications"]
                if row["enabled"] and row["name"] != "Search"
            ],
        )

    def test_cli_authenticates_and_checks_views_without_output(self):
        fixture = json.loads((ROOT / "config" / "expected-catalogs.json").read_text())
        values = {
            "AIOSTREAMS_AUTH": "operator:password",
            "AIOSTREAMS_UUID": "user",
            "AIOSTREAMS_PASSWORD": "password",
            "AIOSTREAMS_PUBLIC_URL": "https://example.com",
        }
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["check-catalogs.py"]),
            patch.object(checker, "Settings") as settings,
            patch.object(checker, "QuietHttp") as constructor,
            contextlib.redirect_stdout(output),
        ):
            settings.return_value.get.side_effect = lambda key, required=False: values[
                key
            ]
            http = constructor.return_value
            http.call.side_effect = [
                {},
                {"data": {"encryptedPassword": "encrypted"}},
                {"catalogs": fixture["manifest"]},
                {"User": {"Id": "user-id"}, "AccessToken": "token"},
                {"Items": [{"Name": name} for name in fixture["views"]]},
            ]
            checker.main()
        calls = http.call.call_args_list
        self.assertEqual(
            [call.args[:2] for call in calls],
            [
                ("POST", "/api/v1/auth/login"),
                ("GET", "/api/v1/user"),
                ("GET", "/stremio/user/encrypted/manifest.json"),
                ("POST", "/jellyfin/Users/AuthenticateByName"),
                ("GET", "/jellyfin/Users/user-id/Views"),
            ],
        )
        self.assertEqual(calls[3].args[2], {"Username": "user", "Pw": "password"})
        self.assertEqual(
            calls[3].kwargs["headers"],
            {"X-Emby-Authorization": checker.EMBY_AUTHORIZATION},
        )
        self.assertEqual(calls[4].kwargs["headers"], {"X-Emby-Token": "token"})
        self.assertEqual(output.getvalue(), "")

    def test_exact_manifest_set_and_view_order(self):
        fixture = json.loads((ROOT / "config" / "expected-catalogs.json").read_text())
        self.assertEqual(
            checker.catalog_differences(
                fixture,
                {"catalogs": list(reversed(fixture["manifest"]))},
                fixture["views"],
            ),
            [],
        )
        differences = checker.catalog_differences(
            fixture,
            {"catalogs": fixture["manifest"][:-1]},
            list(reversed(fixture["views"])),
        )
        self.assertEqual(len(differences), 3)

    def test_duplicates_in_fixture_and_observed(self):
        catalog = {"type": "movie", "id": "row", "name": "Row"}
        for expected, observed in (
            ([catalog, catalog], [catalog]),
            ([catalog], [catalog, catalog]),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(SystemExit, "duplicate"),
            ):
                checker.catalog_differences(
                    {"manifest": expected, "views": []}, {"catalogs": observed}, []
                )

    def test_wrong_name_or_type_is_a_difference(self):
        expected = {"type": "movie", "id": "row", "name": "Row"}
        for observed in ({**expected, "name": "Other"}, {**expected, "type": "series"}):
            self.assertEqual(
                len(
                    checker.catalog_differences(
                        {"manifest": [expected], "views": []},
                        {"catalogs": [observed]},
                        [],
                    )
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
