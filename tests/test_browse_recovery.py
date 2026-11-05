import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import bootstrap
import common
import test_browse_tuning as browse_tests
from test_browse_tuning import BEFORE, TARGET, response


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.snapshots = Path(self.directory.name) / "snapshots"
        self.before = copy.deepcopy(BEFORE)
        self.desired = copy.deepcopy(BEFORE)
        self.desired["jellyfin"]["resolveOnOpen"] = False
        self.settings = Mock()
        self.settings.get.return_value = ""
        self.output = io.StringIO()

    def write(self, http, desired=None):
        with contextlib.redirect_stdout(self.output):
            return bootstrap.guarded_write(
                http,
                {},
                self.before,
                desired or self.desired,
                TARGET,
                self.snapshots,
                settings=self.settings,
            )

    def assert_puts(self, http, count):
        puts = [call for call in http.call.call_args_list if call.args[0] == "PUT"]
        self.assertEqual(len(puts), count)
        self.assertTrue(all(call.kwargs["retry_429"] is False for call in puts))

    def test_unrelated_drift_after_forward_write_is_preserved_by_cli(self):
        case = browse_tests.BrowseTests(
            "test_only_boolean_changes_and_selected_settings_reach_snapshot"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        later = copy.deepcopy(case.desired)
        later["custom"] = {"later_edit": True}
        with self.assertRaisesRegex(SystemExit, "uncertain|unexpected"):
            case.run_cli(
                case.apply_args(),
                [
                    {},
                    response(BEFORE),
                    response(BEFORE),
                    {},
                    response(later),
                    {},
                    response(BEFORE),
                ],
            )
        self.assert_puts(case.http, 1)
        self.assertEqual(len(list(case.snapshots.glob("*.json"))), 1)

    def test_unavailable_post_write_readback_never_authorizes_rollback(self):
        for ambiguous in (False, True):
            http = Mock()
            http.call.side_effect = [
                common.RequestError("PUT", None, True) if ambiguous else {},
                *[common.RequestError("GET", 503)] * 4,
                {},
                response(self.before),
            ]
            with (
                patch.object(common.time, "sleep"),
                self.assertRaisesRegex(SystemExit, "uncertain"),
            ):
                self.write(http)
            self.assert_puts(http, 1)
            self.assertEqual(len(http.call.call_args_list), 5)
            self.assertTrue(any(self.snapshots.glob("*.json")))

    def test_structurally_unusable_post_write_readback_never_authorizes_rollback(self):
        for body in (
            b"fake-body",
            [],
            {},
            {"data": None},
            {"data": {}},
            {"data": {"userData": None}},
            {"data": {"userData": []}},
        ):
            with self.subTest(body=body):
                http = Mock()
                http.call.side_effect = [{}, body, body, body, body]
                with (
                    patch.object(common.time, "sleep"),
                    self.assertRaisesRegex(SystemExit, "uncertain"),
                ):
                    self.write(http)
                self.assert_puts(http, 1)
                self.assertTrue(any(self.snapshots.glob("*.json")))

    def test_rollback_precheck_requires_exact_candidate_and_available_read(self):
        drift = copy.deepcopy(self.desired)
        drift["custom"] = "later edit"
        for readings in (
            [response(drift)],
            [common.RequestError("GET", 503)] * 4,
            [{"data": {"userData": None}}] * 4,
        ):
            with self.subTest(readings=readings):
                http = Mock()
                http.call.side_effect = [*readings, {}, response(self.before)]
                with (
                    patch.object(common.time, "sleep"),
                    self.assertRaisesRegex(
                        SystemExit, "rollback failed|rollback refused"
                    ),
                ):
                    bootstrap.rollback_config(
                        http,
                        {},
                        self.before,
                        TARGET,
                        self.snapshots / "retained.json",
                        "verification failed",
                        expected=self.desired,
                    )
                self.assert_puts(http, 0)

    def test_missing_explicit_rollback_candidate_refuses_without_put(self):
        http = Mock()
        http.call.side_effect = [{}, response(self.before)]
        with self.assertRaisesRegex(SystemExit, "rollback failed|rollback refused"):
            bootstrap.rollback_config(
                http,
                {},
                self.before,
                TARGET,
                self.snapshots / "retained.json",
                "failed",
            )
        self.assert_puts(http, 0)

    def decoder_http(self, malformed, committed):
        state = {"config": copy.deepcopy(self.before)}
        http = common.Http("http://example.invalid")

        def open_request(request, timeout):
            if request.get_method() == "PUT":
                if committed:
                    state["config"] = json.loads(request.data)["config"]
                return io.BytesIO(malformed)
            return io.BytesIO(json.dumps(response(state["config"])).encode())

        http.opener.open = Mock(side_effect=open_request)
        http.call = Mock(wraps=http.call)
        return http, state

    def test_actual_decoder_malformed_forward_committed_and_unchanged(self):
        for malformed in (b"{", b'{"secret":"\xff"}'):
            for committed in (True, False):
                with self.subTest(malformed=malformed, committed=committed):
                    http, state = self.decoder_http(malformed, committed)
                    if committed:
                        self.assertEqual(self.write(http), response(self.desired))
                    else:
                        with self.assertRaisesRegex(SystemExit, "no change was made"):
                            self.write(http)
                    self.assert_puts(http, 1)
                    self.assertEqual(http.opener.open.call_count, 2)
                    self.assertEqual(
                        state["config"], self.desired if committed else self.before
                    )
                    self.assertNotIn("secret", self.output.getvalue())

    def test_actual_decoder_eligible_rollback_committed_and_unchanged(self):
        for malformed in (b"{", b'{"secret":"\xff"}'):
            for committed in (True, False):
                with self.subTest(malformed=malformed, committed=committed):
                    http, state = self.decoder_http(malformed, committed)
                    state["config"] = copy.deepcopy(self.desired)
                    with self.assertRaisesRegex(
                        SystemExit,
                        "rollback verified" if committed else "rollback failed",
                    ):
                        bootstrap.rollback_config(
                            http,
                            {},
                            self.before,
                            TARGET,
                            self.snapshots / "retained.json",
                            "verification failed",
                            expected=self.desired,
                        )
                    self.assertEqual(
                        [call.args[0] for call in http.call.call_args_list],
                        ["GET", "PUT", "GET"],
                    )
                    self.assert_puts(http, 1)
                    self.assertEqual(
                        state["config"], self.before if committed else self.desired
                    )

    def test_actual_decoder_automatic_eligible_rollback_is_single_attempt(self):
        unsafe = copy.deepcopy(self.desired)
        unsafe["presets"][0]["credentials"] = {"token": "replacement"}
        for malformed in (b"{", b'{"secret":"\xff"}'):
            for committed in (True, False):
                state = {"config": copy.deepcopy(self.before), "puts": 0}
                http = common.Http("http://example.invalid")

                def open_request(
                    request,
                    timeout,
                    state=state,
                    committed=committed,
                    malformed=malformed,
                ):
                    if request.get_method() == "PUT":
                        state["puts"] += 1
                        if state["puts"] == 1 or committed:
                            state["config"] = json.loads(request.data)["config"]
                        return io.BytesIO(b"{}" if state["puts"] == 1 else malformed)
                    return io.BytesIO(json.dumps(response(state["config"])).encode())

                http.opener.open = Mock(side_effect=open_request)
                http.call = Mock(wraps=http.call)
                with self.assertRaisesRegex(
                    SystemExit, "rollback verified" if committed else "rollback failed"
                ):
                    self.write(http, unsafe)
                self.assert_puts(http, 2)
                self.assertEqual(
                    [call.args[0] for call in http.call.call_args_list],
                    ["PUT", "GET", "GET", "PUT", "GET"],
                )
                self.assertEqual(state["config"], self.before if committed else unsafe)

    def test_actual_decoder_failures_are_sanitized_and_binary_still_works(self):
        for malformed in (b'{"private-body":', b'{"private-body":"\xff"}'):
            http = common.Http("http://example.invalid")
            http.opener.open = Mock(return_value=io.BytesIO(malformed))
            with self.assertRaises(common.RequestError) as caught:
                http.call("PUT", "/api/v1/user", {}, retry_429=False)
            self.assertTrue(caught.exception.committed_unknown)
            self.assertNotIn("private-body", str(caught.exception))
            self.assertEqual(http.opener.open.call_count, 1)
        http = common.Http("http://example.invalid")
        http.opener.open = Mock(return_value=io.BytesIO(b"\xffraw-binary"))
        self.assertEqual(http.call("GET", "/bytes"), b"\xffraw-binary")

    def test_actual_decoder_malformed_get_exhaustion_never_rolls_back(self):
        http = common.Http("http://example.invalid")
        http.opener.open = Mock(
            side_effect=lambda request, timeout: io.BytesIO(
                b"{}" if request.get_method() == "PUT" else b"{"
            )
        )
        http.call = Mock(wraps=http.call)
        with (
            patch.object(common.time, "sleep"),
            self.assertRaisesRegex(SystemExit, "uncertain"),
        ):
            self.write(http)
        self.assert_puts(http, 1)
        self.assertEqual(http.opener.open.call_count, 5)

    def test_verified_candidate_credential_failure_allows_fresh_checked_rollback(self):
        unsafe = copy.deepcopy(self.desired)
        unsafe["presets"][0]["credentials"] = {"token": "replacement"}
        for later in (unsafe, {**unsafe, "custom": "later edit"}):
            http = Mock()
            http.call.side_effect = [
                {},
                response(unsafe),
                response(later),
                {},
                response(self.before),
            ]
            with self.assertRaisesRegex(
                SystemExit,
                "rollback verified"
                if later == unsafe
                else "rollback failed|rollback refused",
            ):
                self.write(http, unsafe)
            self.assert_puts(http, 2 if later == unsafe else 1)

    def test_eligible_rollback_unavailable_or_malformed_precheck_never_puts(self):
        unsafe = copy.deepcopy(self.desired)
        unsafe["presets"][0]["credentials"] = {"token": "replacement"}
        for readback in (common.RequestError("GET", 503), {"data": {"userData": []}}):
            http = Mock()
            http.call.side_effect = [{}, response(unsafe), *[readback] * 4]
            with (
                patch.object(common.time, "sleep"),
                self.assertRaisesRegex(SystemExit, "rollback failed"),
            ):
                self.write(http, unsafe)
            self.assert_puts(http, 1)
            self.assertEqual(
                [call.args[0] for call in http.call.call_args_list],
                ["PUT", "GET", "GET", "GET", "GET", "GET"],
            )
            self.assertTrue(any(self.snapshots.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
