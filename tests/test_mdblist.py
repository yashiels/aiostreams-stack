import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import bootstrap
import common
import mdblist
from test_bootstrap_rows import HOUSEHOLD, snapshot

ROOT = Path(__file__).resolve().parent.parent
KEY = "test-mdblist-private-value"
TARGET = ("person", "test-uuid", "test-password", False)


def template():
    return json.loads((ROOT / "config" / "aiostreams.template.json").read_text())


def configured(key=KEY, state="on"):
    return mdblist.configure_rows(template(), key, state)


def response(config):
    return {"data": {"userData": config}}


class LoaderTests(unittest.TestCase):
    def test_bindings_order_and_options(self):
        body = template()
        ids = [preset["instanceId"] for preset in body["presets"]]
        start = ids.index("tco") + 1
        self.assertEqual(ids[start : start + 7], list(mdblist.ROWS))
        rows = body["catalogModifications"]
        start = (
            next(i for i, row in enumerate(rows) if row["name"] == "HBO Max Shows") + 1
        )
        self.assertEqual(
            [row["name"] for row in rows[start : start + 7]],
            [row[3] for row in mdblist.ROWS.values()],
        )
        for preset in body["presets"]:
            if preset["instanceId"] not in mdblist.ROWS:
                continue
            row = mdblist.ROWS[preset["instanceId"]]
            self.assertEqual(preset["type"], "custom")
            self.assertTrue(preset["enabled"])
            self.assertEqual(
                preset["options"],
                {
                    "manifestUrl": f"https://{mdblist.HOST}/{row[0]}/{mdblist.TOKEN}/manifest.json",
                    "name": "MDBList " + row[3],
                    "resources": ["catalog"],
                    "timeout": 7000,
                },
            )
        self.assertEqual(json.dumps(body).count(mdblist.TOKEN), 7)
        self.assertNotRegex(json.dumps(body), r"/[A-Za-z0-9]{25}/manifest\.json")

    def test_url_quoted_key_and_nested_substitution(self):
        key = "test/key +?&%é"
        body = template()
        preset = next(p for p in body["presets"] if p["instanceId"] == "gac")
        preset["options"]["nested"] = [{"value": mdblist.TOKEN}]
        desired = mdblist.configure_rows(body, key)
        preset = next(p for p in desired["presets"] if p["instanceId"] == "gac")
        self.assertEqual(
            preset["options"]["manifestUrl"],
            f"https://{mdblist.HOST}/3106/{quote(key, safe='')}/manifest.json",
        )
        self.assertEqual(preset["options"]["nested"][0]["value"], quote(key, safe=""))
        self.assertNotIn(mdblist.TOKEN, json.dumps(desired))
        self.assertIn(mdblist.TOKEN, json.dumps(body))

    def test_missing_key_and_break_glass(self):
        with self.assertRaisesRegex(SystemExit, "MDBLIST_API_KEY is required"):
            configured("")
        for key in ("", KEY):
            body = template()
            body["presets"][0]["enabled"] = False
            desired = mdblist.configure_rows(body, key, "off")
            for before, after in zip(body["presets"], desired["presets"], strict=True):
                if before["instanceId"] in mdblist.ROWS:
                    self.assertFalse(after["enabled"])
                else:
                    self.assertEqual(before, after)
            self.assertEqual(
                body["catalogModifications"], desired["catalogModifications"]
            )
        for state in ("", "on"):
            self.assertTrue(
                next(
                    p
                    for p in configured(state=state)["presets"]
                    if p["instanceId"] == "gac"
                )["enabled"]
            )

    def test_invalid_switches(self):
        for state in ("On", "false", "0", " ", " on", "off "):
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(SystemExit, "MDBLIST_ROWS must be on or off"),
            ):
                configured(state=state)

    def test_rejects_url_variants_and_swapped_lists(self):
        valid = f"https://{mdblist.HOST}/3106/{mdblist.TOKEN}/manifest.json"
        urls = [
            valid.replace("https:", "http:"),
            valid.replace(mdblist.HOST, "example.com"),
            valid.replace(mdblist.HOST, "user@" + mdblist.HOST),
            valid.replace(mdblist.HOST, "user:password@" + mdblist.HOST),
            valid.replace(mdblist.HOST, mdblist.HOST + ":443"),
            valid + "?query=yes",
            valid + "#fragment",
            valid + "?",
            valid + "#",
            valid.replace("3106", "3107"),
            valid.replace("3106", "9999"),
            valid.replace(mdblist.TOKEN, "literal-key"),
            valid.replace(mdblist.TOKEN, mdblist.TOKEN + mdblist.TOKEN),
            valid.replace("/manifest.json", "/extra/manifest.json"),
        ]
        for url in urls:
            body = template()
            next(p for p in body["presets"] if p["instanceId"] == "gac")["options"][
                "manifestUrl"
            ] = url
            with self.subTest(url=url), self.assertRaises(SystemExit):
                mdblist.configure_rows(body, KEY)

    def test_rejects_catalog_binding_changes(self):
        for change in (
            {"type": "series"},
            {"name": "Other"},
            {"id": "gace3b0.comedy-movie"},
        ):
            body = template()
            next(
                row
                for row in body["catalogModifications"]
                if row["name"] == "Action Movies"
            ).update(change)
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(SystemExit, "catalog binding"),
            ):
                mdblist.configure_rows(body, KEY)
        for mutation in ("missing", "duplicate"):
            body = template()
            row = next(
                row
                for row in body["catalogModifications"]
                if row["name"] == "Action Movies"
            )
            if mutation == "missing":
                body["catalogModifications"].remove(row)
            else:
                body["catalogModifications"].append(copy.deepcopy(row))
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit):
                mdblist.configure_rows(body, KEY)

    def test_rejects_unknown_identity_type_and_duplicate(self):
        for change in ({"instanceId": "unknown"}, {"type": "other"}, {}):
            body = template()
            preset = next(p for p in body["presets"] if p["instanceId"] == "gac")
            if change:
                preset.update(change)
            else:
                body["presets"].append(copy.deepcopy(preset))
            with self.subTest(change=change), self.assertRaises(SystemExit):
                mdblist.configure_rows(body, KEY)

    def test_rotation_swap_paths_and_removal_guard(self):
        before = configured("old-private-key")
        after = configured()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bootstrap.print_sync_diff(before, after, after)
        self.assertIn("changed: presets[gac].options.manifestUrl", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        self.assertNotIn("old-private-key", output.getvalue())
        swapped = copy.deepcopy(after)
        next(p for p in swapped["presets"] if p["instanceId"] == "gac")["options"][
            "manifestUrl"
        ] = f"https://{mdblist.HOST}/3107/{KEY}/manifest.json"
        self.assertIn(
            "presets[gac].options.manifestUrl",
            bootstrap.preset_differences(after, swapped)[2],
        )
        removed = copy.deepcopy(after)
        removed["presets"] = [p for p in removed["presets"] if p["instanceId"] != "gac"]
        with self.assertRaisesRegex(SystemExit, "allow-preset-removal"):
            bootstrap.sync_template(after, removed)
        self.assertEqual(
            bootstrap.sync_template(after, removed, True)["presets"], removed["presets"]
        )

    def test_jellyfin_library_limit_matches_expected_view_count(self):
        service = None
        in_environment = False
        limits = []
        for line in (ROOT / "compose.yml").read_text().splitlines():
            indentation = len(line) - len(line.lstrip())
            if indentation == 2:
                service = line.strip().removesuffix(":")
                in_environment = False
            elif indentation == 4:
                in_environment = line.strip() == "environment:"
            elif service == "aiostreams" and in_environment and indentation == 6:
                name, separator, value = line.strip().partition(":")
                if separator and name == "JELLYFIN_MAX_LIBRARIES":
                    limits.append(json.loads(value.strip()))
        fixture = json.loads((ROOT / "config" / "expected-catalogs.json").read_text())
        self.assertEqual(limits, [str(len(fixture["views"]))])

    def test_image_is_digest_pinned(self):
        lines = (ROOT / "versions.env").read_text().splitlines()
        image = next(
            line.partition("=")[2]
            for line in lines
            if line.startswith("AIOSTREAMS_IMAGE=")
        )
        self.assertRegex(image, r"^[^@]+:[^@]+@sha256:[0-9a-f]{64}$")


class SnapshotTests(unittest.TestCase):
    def test_save_without_key_is_tokenized_and_source_unchanged(self):
        before = configured()
        original = copy.deepcopy(before)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"MDBLIST_API_KEY": ""}),
        ):
            path = common.snapshot_path(directory, TARGET[0])
            common.save_snapshot(path, before, TARGET)
            self.assertNotIn(KEY, path.read_text())
            self.assertEqual(path.read_text().count(mdblist.TOKEN), 7)
            saved = json.loads(path.read_text())["config"]
            self.assertEqual(mdblist.restored_config(saved, KEY), before)
            with self.assertRaisesRegex(SystemExit, "required to restore"):
                mdblist.restored_config(saved, "")
        self.assertEqual(before, original)

    def test_fail_closed_before_file_open(self):
        cases = []
        body = configured()
        next(p for p in body["presets"] if p["instanceId"] == "gac")["options"][
            "manifestUrl"
        ] += "?unsafe=true"
        cases.append(body)
        body = configured()
        next(p for p in body["presets"] if p["instanceId"] == "gac")["instanceId"] = (
            "unknown"
        )
        cases.append(body)
        body = configured()
        body["unexpected"] = KEY
        cases.append(body)
        for body in cases:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(common.os, "open") as opener,
                self.assertRaises(SystemExit),
            ):
                common.save_snapshot(Path(directory) / "save.json", body, TARGET, KEY)
            opener.assert_not_called()

    def test_environment_key_leak_is_rejected_before_open(self):
        body = configured()
        body["unexpected"] = KEY
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"MDBLIST_API_KEY": KEY}),
            patch.object(common.os, "open") as opener,
            self.assertRaisesRegex(SystemExit, "credential"),
        ):
            common.save_snapshot(Path(directory) / "save.json", body, TARGET)
        opener.assert_not_called()

    def test_save_cli_without_key_uses_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            values = {
                "AIOSTREAMS_AUTH": "admin:password",
                "AIOSTREAMS_PUBLIC_URL": "https://example.com",
            }
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["config-snapshot.py", "save", directory]),
                patch.object(snapshot, "resolve_target", return_value=TARGET),
                patch.object(snapshot, "Settings") as settings,
                patch.object(snapshot, "QuietHttp") as constructor,
                contextlib.redirect_stdout(output),
            ):
                settings.return_value.get.side_effect = lambda name, required=False: (
                    values.get(name, "")
                )
                constructor.return_value.call.side_effect = [{}, response(configured())]
                snapshot.main()
            body = Path(output.getvalue().strip()).read_text()
            self.assertNotIn(KEY, body)
            self.assertEqual(body.count(mdblist.TOKEN), 7)
            self.assertNotIn(KEY, output.getvalue())

    def test_quoted_secret_elsewhere_fails_closed(self):
        key = "private/key+value"
        body = configured(key)
        body["unexpected"] = quote(key, safe="")
        with self.assertRaisesRegex(SystemExit, "credential"):
            mdblist.tokenized_config(body, key)

    def test_retention_is_per_target_and_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keep = root / "person-not-a-snapshot.json"
            keep.write_text("keep")
            other = common.snapshot_path(root, "other")
            common.save_snapshot(other, {}, ("other", "other-id"))
            paths = []
            for _ in range(12):
                path = common.snapshot_path(root, TARGET[0])
                common.save_snapshot(path, configured(), TARGET, KEY)
                paths.append(path)
            self.assertEqual([p.exists() for p in paths], [False, False] + [True] * 10)
            self.assertTrue(other.exists())
            self.assertTrue(keep.exists())
            for path in paths[2:]:
                self.assertNotIn(KEY, path.read_text())

    def test_restore_cli_requires_key_before_http_and_rotates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = common.snapshot_path(directory, TARGET[0])
            common.save_snapshot(path, configured(), TARGET)
            for key in ("", "replacement-private-key"):
                values = {
                    "MDBLIST_API_KEY": key,
                    "AIOSTREAMS_AUTH": "admin:password",
                    "AIOSTREAMS_PUBLIC_URL": "https://example.com",
                }
                with (
                    patch.object(
                        sys, "argv", ["config-snapshot.py", "restore", str(path)]
                    ),
                    patch.object(snapshot, "resolve_target", return_value=TARGET),
                    patch.object(snapshot, "Settings") as settings,
                    patch.object(snapshot, "QuietHttp") as constructor,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    settings.return_value.get.side_effect = (
                        lambda name, required=False, values=values: values.get(name, "")
                    )
                    if not key:
                        with self.assertRaisesRegex(SystemExit, "required to restore"):
                            snapshot.main()
                        constructor.assert_not_called()
                    else:
                        desired = configured(key)
                        constructor.return_value.call.side_effect = [
                            {},
                            {},
                            response(desired),
                        ]
                        snapshot.main()
                        self.assertEqual(
                            constructor.return_value.call.call_args_list[1].args[2],
                            {"config": desired},
                        )


class HostClassificationTests(unittest.TestCase):
    def invalid_presets(self, segment):
        url = f"https://{mdblist.HOST}/3106/{segment}/manifest.json"
        valid = {
            "instanceId": "gac",
            "type": "custom",
            "enabled": True,
            "options": {"manifestUrl": url},
        }
        return [
            {**valid, "instanceId": "unknown", "type": "other"},
            {**valid, "instanceId": "unknown"},
            {**valid, "type": "other"},
            {
                "instanceId": "unknown",
                "type": "other",
                "options": {"nested": [{"url": url}]},
            },
            {
                "instanceId": "unknown",
                "type": "other",
                "options": {"description": "See " + url + " for details"},
            },
            {**valid, "options": {"manifestUrl": url, "nested": [{"url": url}]}},
            {"instanceId": "unknown", "type": "other", "name": url},
            {
                "instanceId": "unknown",
                "type": "other",
                "options": {"url": "https://example.com/?redirect=" + url},
            },
            {**valid, "options": {"manifestUrl": url.replace("3106", "3107")}},
            {**valid, "options": {"manifestUrl": url + "?invalid=yes"}},
            *[
                preset
                for variant in (
                    url.replace(mdblist.HOST, mdblist.HOST + "."),
                    url.replace(mdblist.HOST, mdblist.HOST.upper()),
                    url.replace(mdblist.HOST, mdblist.HOST + ":443"),
                )
                for preset in (
                    {
                        "instanceId": "unknown",
                        "type": "other",
                        "options": {"manifestUrl": variant},
                    },
                    {**valid, "options": {"manifestUrl": variant}},
                )
            ],
        ]

    def test_snapshot_rejects_all_host_presets_before_open_with_rotated_key(self):
        old_key = "old-mdblist-private-value"
        for key in ("", KEY):
            for preset in self.invalid_presets(old_key):
                with (
                    self.subTest(key_set=bool(key), preset=preset),
                    tempfile.TemporaryDirectory() as directory,
                    patch.dict(os.environ, {"MDBLIST_API_KEY": key}),
                    patch.object(common.os, "open") as opener,
                ):
                    path = Path(directory) / "save.json"
                    with self.assertRaises(SystemExit) as caught:
                        common.save_snapshot(path, {"presets": [preset]}, TARGET)
                    opener.assert_not_called()
                    self.assertFalse(path.exists())
                    self.assertNotIn(old_key, str(caught.exception))
                    self.assertNotIn(KEY, str(caught.exception))

    def test_template_rejects_all_host_presets_even_when_rows_are_off(self):
        for segment in (mdblist.TOKEN, "old-mdblist-private-value"):
            for key in ("", KEY):
                for preset in self.invalid_presets(segment):
                    with (
                        self.subTest(segment=segment, key_set=bool(key), preset=preset),
                        self.assertRaises(SystemExit),
                    ):
                        mdblist.configure_rows({"presets": [preset]}, key, "off")

    def test_valid_managed_url_tokenizes_independent_of_current_key(self):
        old_key = "old-mdblist-private-value"
        for key in ("", KEY):
            body = mdblist.tokenized_config(configured(old_key), key)
            self.assertNotIn(old_key, json.dumps(body))
            self.assertEqual(json.dumps(body).count(mdblist.TOKEN), 7)

    def test_other_host_urls_remain_unmanaged(self):
        preset = {
            "instanceId": "unknown",
            "type": "other",
            "options": {"nested": [{"url": "https://example.com/value"}]},
        }
        self.assertIsNone(mdblist.managed_segment(preset))
        self.assertEqual(
            mdblist.tokenized_config({"presets": [preset]}), {"presets": [preset]}
        )


class MainTests(unittest.TestCase):
    def run_main(self, mode, key=KEY, state="on", dry=False):
        before = (
            configured("old-private-key") if mode == "--sync-template" else template()
        )
        if mode == "--apply-rows":
            before["presets"] = [
                p for p in before["presets"] if p["instanceId"] not in mdblist.ROWS
            ]
        elif mode == "--apply-template":
            before = configured("old-private-key")
        desired = configured(key, state)
        digest = (
            bootstrap.sync_sha256(before, desired)
            if mode == "--sync-template"
            else bootstrap.rows_sha256(before)
        )
        values = {
            "MDBLIST_API_KEY": key,
            "MDBLIST_ROWS": state,
            "AIOSTREAMS_AUTH": "admin:password",
            "AIOSTREAMS_PUBLIC_URL": "https://example.com",
            "TORBOX_API_KEY": "test-torbox",
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SNAPSHOT_DIR": directory}),
            patch.object(
                sys,
                "argv",
                [
                    "bootstrap.py",
                    mode,
                    *(
                        ["--dry-run", "--diff"]
                        if dry
                        else ["--expect-sha256", digest]
                        if mode in ("--sync-template", "--apply-rows")
                        else []
                    ),
                ],
            ),
            patch.object(bootstrap, "resolve_target", return_value=TARGET),
            patch.object(bootstrap, "Settings") as settings,
            patch.object(bootstrap, "QuietHttp") as constructor,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            settings.return_value.get.side_effect = (
                lambda name, required=False, values=values: values.get(name, "")
            )
            constructor.return_value.call.side_effect = [
                {},
                response(before),
                {},
                response(desired),
            ]
            bootstrap.main()
            calls = constructor.return_value.call.call_args_list
            for path in Path(directory).glob("*.json"):
                self.assertNotIn(key, path.read_text())
        self.assertNotIn(key, stdout.getvalue() + stderr.getvalue())
        return calls

    def test_all_existing_write_modes_use_substituted_template(self):
        for mode in ("--sync-template", "--apply-rows", "--apply-template"):
            with self.subTest(mode=mode):
                calls = self.run_main(mode)
                body = next(
                    call.args[2]["config"] for call in calls if call.args[0] == "PUT"
                )
                self.assertNotIn(mdblist.TOKEN, json.dumps(body))
                self.assertIn(KEY, json.dumps(body))

    def test_off_sync_writes_disabled_presets(self):
        calls = self.run_main("--sync-template", state="off")
        body = next(call.args[2]["config"] for call in calls if call.args[0] == "PUT")
        self.assertTrue(
            all(
                not p["enabled"]
                for p in body["presets"]
                if p["instanceId"] in mdblist.ROWS
            )
        )

    def test_dry_diff_has_no_secret(self):
        self.assertFalse(
            any(
                call.args[0] == "PUT"
                for call in self.run_main("--sync-template", dry=True)
            )
        )

    def test_invalid_state_before_http(self):
        for state, key in (
            ("On", KEY),
            ("false", KEY),
            ("0", KEY),
            (" ", KEY),
            ("on", ""),
        ):
            values = {
                "MDBLIST_ROWS": state,
                "MDBLIST_API_KEY": key,
                "AIOSTREAMS_AUTH": "admin:password",
                "AIOSTREAMS_PUBLIC_URL": "https://example.com",
            }
            with (
                patch.object(
                    sys, "argv", ["bootstrap.py", "--sync-template", "--dry-run"]
                ),
                patch.object(bootstrap, "resolve_target", return_value=TARGET),
                patch.object(bootstrap, "Settings") as settings,
                patch.object(bootstrap, "QuietHttp") as constructor,
                self.assertRaisesRegex(SystemExit, "MDBLIST"),
            ):
                settings.return_value.get.side_effect = (
                    lambda name, required=False, values=values: values.get(name, "")
                )
                bootstrap.main()
            constructor.assert_not_called()

    def test_create_uses_substituted_template_and_torbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "household.json").write_text(json.dumps(HOUSEHOLD))
            (root / "config" / "aiostreams.template.json").write_text(
                json.dumps(template())
            )
            values = {
                "MDBLIST_API_KEY": KEY,
                "MDBLIST_ROWS": "on",
                "TORBOX_API_KEY": "test-torbox",
                "AIOSTREAMS_AUTH": "admin:password",
                "AIOSTREAMS_PUBLIC_URL": "https://example.com",
            }
            output = io.StringIO()
            with (
                patch.object(sys, "argv", ["bootstrap.py"]),
                patch.object(bootstrap, "ROOT", root),
                patch.object(
                    bootstrap, "resolve_target", return_value=("main", "", "", True)
                ),
                patch.object(bootstrap, "Settings") as settings,
                patch.object(bootstrap, "QuietHttp") as constructor,
                contextlib.redirect_stdout(output),
            ):
                settings.return_value.get.side_effect = (
                    lambda name, required=False, values=values: values.get(name, "")
                )
                constructor.return_value.call.side_effect = [
                    {},
                    {
                        "data": {
                            "uuid": "test-uuid",
                            "encryptedPassword": "test-encrypted",
                        }
                    },
                ]
                bootstrap.main()
            body = constructor.return_value.call.call_args_list[1].args[2]["config"]
            self.assertNotIn(mdblist.TOKEN, json.dumps(body))
            self.assertIn(KEY, json.dumps(body))
            self.assertEqual(
                next(s for s in body["services"] if s["id"] == "torbox")["credentials"],
                {"apiKey": "test-torbox"},
            )
            self.assertNotIn(KEY, output.getvalue())

    def test_conflict_http_failure_and_rollback_do_not_print_key(self):
        current, desired = configured("old-private-key"), configured()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                bootstrap.apply_rows(current, desired)
            print(str(caught.exception), file=sys.stderr)
            http = common.QuietHttp("https://example.com")
            error = urllib.error.HTTPError(
                f"https://example.com/{KEY}", 403, KEY, {}, io.BytesIO(KEY.encode())
            )
            with patch.object(http.opener, "open", side_effect=error):
                with self.assertRaises(common.RequestError) as caught:
                    http.call("PUT", "/" + KEY, {"key": KEY})
                print(str(caught.exception), file=sys.stderr)
            for failure in ("http", "rollback", "rollback-fails"):
                client = Mock()
                if failure == "http":
                    client.call.side_effect = [
                        common.RequestError("PUT", 403),
                        response(current),
                    ]
                elif failure == "rollback":
                    client.call.side_effect = [{}, response({}), {}, response(current)]
                else:
                    client.call.side_effect = [
                        {},
                        response({}),
                        common.RequestError("PUT", 403),
                    ]
                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(bootstrap, "Settings") as settings,
                ):
                    settings.return_value.get.return_value = KEY
                    with self.assertRaises(SystemExit) as caught:
                        bootstrap.guarded_write(
                            client, {}, current, desired, TARGET, directory
                        )
                    print(str(caught.exception), file=sys.stderr)
                    saved = next(Path(directory).glob("*.json")).read_text()
                    self.assertNotIn(KEY, saved)
                    self.assertNotIn("old-private-key", saved)
        for key in (KEY, "old-private-key", mdblist.TOKEN):
            self.assertNotIn(key, output.getvalue() + errors.getvalue())
        self.assertIn("rollback verified", errors.getvalue())
        self.assertIn("rollback failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
