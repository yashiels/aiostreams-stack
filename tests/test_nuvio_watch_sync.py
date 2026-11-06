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
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
from common import RequestError

SPEC = importlib.util.spec_from_file_location(
    "nuvio_watch_sync",
    Path(__file__).resolve().parent.parent / "ops/nuvio-watch-sync.py",
)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
SCOPE = "12345678-abcd-abcd-abcd-123456789abc".replace("-", "")


def native(kind="movie", identifier="tmdb:101", played=1, at=200, duration=1000):
    return {
        "item_key": identifier,
        "kind": kind,
        "media_type": "movie" if kind == "movie" else "series",
        "base_id": identifier,
        "season": None if kind == "movie" else 2,
        "episode": None if kind == "movie" else 3,
        "video_id": identifier if kind == "movie" else "tt7654321:2:3",
        "match_key": "m|tt7654321",
        "position_ms": 400,
        "duration_ms": duration,
        "played": played,
        "last_played_at": at,
        "updated_at": at,
        "origin": "local",
        "snapshot": json.dumps({"name": "Example", "seriesName": "Example Series"}),
    }


def export(*rows):
    return {
        "scope": {"uuid": SCOPE, "persona": ""},
        "counts": {
            "total": len(rows) + 1,
            "watched": sum(r["played"] for r in rows),
            "resumable": sum(not r["played"] for r in rows),
        },
        "rows": list(rows),
    }


def mapped(*rows):
    return sync.source_rows(export(*rows), SCOPE)


def history(identifier="tmdb:101", at=100):
    return {
        "content_id": identifier,
        "content_type": "movie",
        "title": "Cloud title",
        "season": None,
        "episode": None,
        "watched_at": at,
    }


def progress(identifier="tmdb:101", at=100, duration=1000):
    return {
        "content_id": identifier,
        "content_type": "movie",
        "video_id": identifier,
        "season": None,
        "episode": None,
        "position": 100,
        "duration": duration,
        "last_watched": at,
        "progress_key": identifier,
    }


def indexed(rows, resource):
    return sync.cloud_rows(rows, resource)


class FakeProviders:
    def __init__(self):
        self.profile = {
            "profile_index": 1,
            "name": "Example Profile",
            "id": "profile-example",
        }
        self.connection = {
            "provider_id": "jellyfin",
            "enabled": True,
            "import_watch_state": True,
            "address": "https://native.example",
            "remote_user_id": SCOPE,
            "token": "synthetic-native-token",
        }
        self.user = {"Id": SCOPE, "Name": "Example User"}
        self.watched = [history("tt1234567", 300)]
        self.progress = [progress("tmdb:909", 300)]
        self.calls = []
        self.uncertain = False
        self.drop_write = False
        self.after_push = None
        self.source = export(native(), native("episode", "tmdb:202", 0, duration=0))

    def http(self, base):
        providers = self

        class Client:
            def call(self, method, path, body=None, **kwargs):
                return providers.call(method, path, body, **kwargs)

        return Client()

    def call(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body), kwargs))
        if path == "/config.js":
            return b'window.CONFIG = {"supabaseUrl":"https://cloud.example","supabaseAnonKey":"synthetic-anon"};'
        if path.startswith("/auth/v1/token"):
            return {"access_token": "synthetic-session"}
        if path == "/auth/v1/user":
            return {"email": "example@example.com"}
        if path == "/Users/Me":
            return copy.deepcopy(self.user)
        name = path.removeprefix("/rest/v1/rpc/sync_")
        if name == "pull_profiles":
            return [copy.deepcopy(self.profile)]
        if name == "pull_media_servers":
            return [{"servers_json": [copy.deepcopy(self.connection)]}]
        if name == "pull_watched_items":
            start = (body["p_page"] - 1) * body["p_page_size"]
            return copy.deepcopy(self.watched[start : start + body["p_page_size"]])
        if name == "pull_watch_progress":
            return copy.deepcopy(self.progress[: body["p_limit"]])
        if name in ("push_watched_items", "push_watch_progress"):
            if not self.drop_write:
                if name == "push_watched_items":
                    self.watched = copy.deepcopy(body["p_items"])
                else:
                    self.progress = copy.deepcopy(body["p_entries"])
            if self.after_push:
                self.after_push(self)
            if self.uncertain:
                raise RequestError("POST")
            return None
        raise AssertionError(path)

    def run_source(self, command, **kwargs):
        self.source_command = command
        self.source_input = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, json.dumps(self.source), "")

    def writes(self):
        return [c for c in self.calls if "/sync_push_" in c[1]]


def run_cli(providers, directory, apply=False):
    output = io.StringIO()
    argv = [
        "nuvio-watch-sync.py",
        "--profile-id",
        "1",
        "--profile-name",
        "Example Profile",
        "--native-user",
        "Example User",
        "--ssh-host",
        "example-host",
        "--container",
        "example-container",
        "--state-dir",
        directory,
    ]
    if apply:
        argv.append("--apply")
    with (
        patch.object(sys, "argv", argv),
        patch.dict(
            os.environ,
            {
                "NUVIO_EMAIL": "example@example.com",
                "NUVIO_PASSWORD": "synthetic-password",
            },
        ),
        patch.object(sync, "Http", side_effect=providers.http),
        patch.object(sync.subprocess, "run", side_effect=providers.run_source),
        contextlib.redirect_stdout(output),
    ):
        sync.main()
    return output.getvalue()


class MergeTests(unittest.TestCase):
    def test_preserves_unrelated_and_newer_state_and_replays(self):
        source = mapped(
            native(),
            native(identifier="tmdb:202", at=200),
            native(identifier="tmdb:303", played=0, at=200),
            native(identifier="tt4444", played=0, at=400),
        )
        watched = indexed([history(at=300), history("tmdb:999")], "watched_items")
        current_progress = indexed(
            [
                progress(at=300),
                progress("tmdb:202"),
                progress("tmdb:303", at=300),
                progress("tmdb:999"),
                progress("tt4444"),
            ],
            "watch_progress",
        )
        desired_watched, desired_progress = sync.merge(
            source, watched, current_progress
        )
        for row in watched.values():
            self.assertEqual(desired_watched[sync.identity(row)], row)
        for identifier in ("tmdb:101", "tmdb:303", "tmdb:999"):
            key = sync.identity(progress(identifier))
            self.assertEqual(desired_progress[key], current_progress[key])
        completed = desired_progress[sync.identity(progress("tmdb:202"))]
        self.assertEqual(
            (completed["position"], completed["last_watched"]), (1000, 200)
        )
        replay = desired_progress[sync.identity(progress("tt4444"))]
        self.assertEqual((replay["position"], replay["last_watched"]), (400, 400))
        self.assertEqual(
            sync.merge(source, desired_watched, desired_progress),
            (desired_watched, desired_progress),
        )

    def test_authoritative_episode_ids_unknown_duration_and_no_completed_progress_creation(
        self,
    ):
        source = mapped(
            native(),
            native("episode", "tmdb:202", 0, 123, 0),
            native(identifier="tt1234567", at=456),
        )
        watched, positions = sync.merge(source, {}, {})
        self.assertEqual(len(watched), 2)
        self.assertEqual(len(positions), 1)
        row = next(iter(positions.values()))
        self.assertEqual(
            row,
            {
                "content_id": "tmdb:202",
                "content_type": "series",
                "video_id": "tt7654321:2:3",
                "season": 2,
                "episode": 3,
                "position": 400,
                "duration": 0,
                "last_watched": 123,
                "progress_key": "tmdb:202_s2e3",
            },
        )
        self.assertEqual(
            watched[sync.identity(history("tt1234567"))]["watched_at"], 456
        )
        complete = mapped(native(duration=0))
        old = indexed([progress(duration=0)], "watch_progress")
        self.assertEqual(sync.merge(complete, {}, old)[1], old)

    def test_invalid_or_incomplete_source_refused(self):
        changes = [
            lambda e: e["scope"].update(persona="hashed-persona"),
            lambda e: e["scope"].update(uuid="wrong"),
            lambda e: e["counts"].update(watched=2),
            lambda e: e["counts"].update(total=0),
            lambda e: e["rows"][0].update(base_id="guessed-title"),
            lambda e: e["rows"][0].update(kind="episode", season=None, episode=1),
            lambda e: e["rows"][0].update(last_played_at=None),
            lambda e: e["rows"][0].update(snapshot="{}"),
            lambda e: e["rows"][0].update(position_ms=-1),
            lambda e: e["rows"][0].update(updated_at=200.5),
            lambda e: e["rows"][0].update(last_played_at=200.5),
            lambda e: e["counts"].update(watched=1.0),
        ]
        for change in changes:
            value = export(native())
            change(value)
            with self.subTest(change=change), self.assertRaises(SystemExit):
                sync.source_rows(value, SCOPE)

    def test_fractional_source_milliseconds_normalized_and_invalid_numbers_refused(
        self,
    ):
        for duration in (6268543.333, 2268056.75, 1788744.75, 8349396.000000001):
            for use_runtime in (False, True):
                row = native(played=0, duration=0 if use_runtime else duration)
                row["position_ms"] = 400.75
                if use_runtime:
                    row["snapshot"] = json.dumps(
                        {"name": "Example", "runtimeMs": duration}
                    )
                with self.subTest(duration=duration, use_runtime=use_runtime):
                    _, positions = sync.merge(mapped(row), {}, {})
                    entry = next(iter(positions.values()))
                    self.assertIs(type(entry["duration"]), int)
                    self.assertIs(type(entry["position"]), int)
                    self.assertEqual(
                        (entry["position"], entry["duration"]), (400, int(duration))
                    )
        for field in ("position_ms", "duration_ms", "runtimeMs"):
            for value in (
                -0.5,
                -1,
                float("nan"),
                float("inf"),
                float("-inf"),
                True,
                "100",
                None,
            ):
                row = native(played=0, duration=0)
                if field == "runtimeMs":
                    row["snapshot"] = json.dumps({"name": "Example", field: value})
                else:
                    row[field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(SystemExit),
                ):
                    mapped(row)


class CliTests(unittest.TestCase):
    def test_dry_apply_readback_and_second_noop(self):
        providers = FakeProviders()
        with tempfile.TemporaryDirectory() as directory:
            dry = run_cli(providers, directory)
            self.assertFalse(providers.writes())
            self.assertIn("dry-run: no writes", dry)
            applied = run_cli(providers, directory, True)
            self.assertIn("apply verified", applied)
            self.assertEqual(len(providers.writes()), 2)
            again = run_cli(providers, directory, True)
            self.assertIn("planned: watched 0; progress 0", again)
            self.assertEqual(len(providers.writes()), 2)
        self.assertEqual(providers.source_input, json.dumps({"uuid": SCOPE}))
        self.assertIn("readOnly:true", providers.source_command[-1])
        self.assertEqual(providers.source_command[:2], ["ssh", "-o"])
        for call in providers.writes():
            self.assertFalse(call[3]["retry_429"])
        self.assertIn(history("tt1234567", 300), providers.watched)
        self.assertIn(progress("tmdb:909", 300), providers.progress)
        for secret in (
            "synthetic-session",
            "synthetic-native-token",
            "example@example.com",
        ):
            self.assertNotIn(secret, dry + applied + again)

    def test_wrong_persona_and_invalid_source_cannot_write(self):
        for change in (
            lambda p: p.connection.update(remote_user_id="persona-hash"),
            lambda p: p.user.update(Id="87654321-abcd-abcd-abcd-123456789abc"),
            lambda p: p.user.update(Name="Other User"),
            lambda p: p.profile.update(name="Other Profile"),
            lambda p: p.source["counts"].update(watched=100),
        ):
            providers = FakeProviders()
            change(providers)
            with (
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises(SystemExit),
            ):
                run_cli(providers, directory, True)
            self.assertFalse(providers.writes())

    def test_uncertain_write_reconciles_once_or_fails_without_retry(self):
        for dropped in (False, True):
            providers = FakeProviders()
            providers.uncertain = True
            providers.drop_write = dropped
            with tempfile.TemporaryDirectory() as directory:
                if dropped:
                    with self.assertRaisesRegex(SystemExit, "readback mismatch"):
                        run_cli(providers, directory, True)
                    self.assertEqual(len(providers.writes()), 1)
                else:
                    self.assertIn(
                        "uncertain write reconciled",
                        run_cli(providers, directory, True),
                    )
                    self.assertEqual(len(providers.writes()), 2)

    def test_drift_and_readback_divergence_fail_without_retry(self):
        providers = FakeProviders()
        original = providers.call
        pulls = 0

        def drift(method, path, body=None, **kwargs):
            nonlocal pulls
            if path.endswith("sync_pull_watched_items"):
                pulls += 1
                if pulls == 2:
                    providers.watched.append(history("tmdb:888", 999))
            return original(method, path, body, **kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(providers, "call", side_effect=drift),
            self.assertRaisesRegex(SystemExit, "cloud state drift"),
        ):
            run_cli(providers, directory, True)
        self.assertFalse(providers.writes())
        providers = FakeProviders()
        providers.after_push = lambda p: p.watched.append(history("tmdb:888", 999))
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(SystemExit, "readback mismatch"),
        ):
            run_cli(providers, directory, True)
        self.assertEqual(len(providers.writes()), 1)

    def test_cloud_pagination_and_limit_boundary(self):
        providers = FakeProviders()
        providers.watched = [history(f"tmdb:{i + 1}") for i in range(901)]
        with tempfile.TemporaryDirectory() as directory:
            run_cli(providers, directory)
        pages = [
            c[2]["p_page"]
            for c in providers.calls
            if c[1].endswith("sync_pull_watched_items")
        ]
        self.assertEqual(pages, [1, 2])
        providers.watched[900] = providers.watched[0]
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(SystemExit, "duplicate"),
        ):
            run_cli(providers, directory, True)
        self.assertFalse(providers.writes())
        providers = FakeProviders()
        providers.watched = [history("tmdb:101"), history("tmdb:202")]
        original = providers.call

        def repeated_page(method, path, body=None, **kwargs):
            result = original(method, path, body, **kwargs)
            if path.endswith("sync_pull_watched_items"):
                if body["p_page"] > 2:
                    raise RuntimeError("unexpected third page request")
                return copy.deepcopy(providers.watched)
            return result

        with (
            patch.object(sync, "PAGE_SIZE", 2),
            patch.object(providers, "call", side_effect=repeated_page),
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(SystemExit, "duplicate"),
        ):
            run_cli(providers, directory, True)
        self.assertEqual(
            [
                c[2]["p_page"]
                for c in providers.calls
                if c[1].endswith("sync_pull_watched_items")
            ],
            [1, 2],
        )
        self.assertFalse(providers.writes())
        providers.watched = []
        providers.progress = [progress(f"tmdb:{i + 1}") for i in range(3)]
        with (
            patch.object(sync, "PROGRESS_LIMIT", 3),
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(SystemExit, "truncated"),
        ):
            run_cli(providers, directory, True)
        self.assertFalse(providers.writes())


if __name__ == "__main__":
    unittest.main()
