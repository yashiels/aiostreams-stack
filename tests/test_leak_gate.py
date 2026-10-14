import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get("M0_TEST_SCRATCH", tempfile.gettempdir())).resolve()


class LeakGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="leak-git-", dir=SCRATCH)).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
            GIT_AUTHOR_DATE="2001-01-01T00:00:00+0000",
            GIT_COMMITTER_DATE="2001-01-01T00:00:00+0000",
        )
        self.git(
            "init",
            "--quiet",
            "--object-format=sha1",
            "--initial-branch=main",
            str(self.root),
        )
        (self.root / "ops").mkdir()
        for name in ("leak-gate.sh", "leak-patterns.txt"):
            shutil.copy2(ROOT / "ops" / name, self.root / "ops" / name)
        self.base = self.commit(b"base\n")
        self.first = self.commit(b"base tip\n", (self.base,))
        self.second = self.commit(b"head tip\n", (self.base,))

    def git(self, *arguments, data=None, identity=None):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            env=dict(self.env, **(identity or {})),
            input=data,
            capture_output=True,
            check=True,
        ).stdout

    def commit(self, message, parents=(), content=b"safe\n", identity=None):
        (self.root / "content.txt").write_bytes(content)
        blob = self.git("hash-object", "-w", "--stdin", data=content).strip()
        tree = (
            self.git("mktree", data=b"100644 blob " + blob + b"\tcontent.txt\n")
            .strip()
            .decode()
        )
        arguments = ["commit-tree", tree]
        for parent in parents:
            arguments += ["-p", parent]
        commit = self.git(*arguments, data=message, identity=identity).strip().decode()
        self.git("update-ref", "refs/heads/main", commit)
        headers, actual_message = self.git("cat-file", "commit", commit).split(
            b"\n\n", 1
        )
        actual_parents = [
            line.removeprefix(b"parent ").decode()
            for line in headers.splitlines()
            if line.startswith(b"parent ")
        ]
        self.assertEqual(actual_parents, list(parents))
        self.assertEqual(actual_message, message)
        return commit

    def canonical(self):
        return f"Merge {self.second} into {self.first}".encode()

    def gate(self):
        return subprocess.run(
            ["bash", "ops/leak-gate.sh"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def assert_blocked(self, label):
        result = self.gate()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(label, result.stderr)
        self.assertEqual(result.stdout, "")
        return result

    def pattern(self):
        return next(
            base64.b64decode(line)
            for line in (ROOT / "ops/leak-patterns.txt").read_bytes().splitlines()
            if line
        )

    def test_canonical_synthetic_merge_uses_actual_parents_and_complete_message(self):
        for suffix in (b"", b"\n"):
            with self.subTest(terminal_newline=bool(suffix)):
                self.commit(self.canonical() + suffix, (self.first, self.second))
                result = self.gate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout + result.stderr, "")

    def test_ordinary_metadata_hex_lengths_remain_blocked(self):
        for length in (32, 40, 64):
            with self.subTest(length=length):
                self.commit(b"note " + b"a" * length + b"\n", (self.base,))
                self.assert_blocked("git metadata:")

    def test_extra_merge_text_and_body_remain_blocked(self):
        canonical = self.canonical()
        for message in (
            canonical + b" extra\n",
            b"prefix " + canonical + b"\n",
            canonical + b"\n\n",
            canonical + b"\n\nbody\n",
            canonical + b"\r\n",
            canonical.replace(b" into ", b"\ninto ") + b"\n",
            *(canonical + b"\n\n" + b"a" * length + b"\n" for length in (32, 40, 64)),
        ):
            with self.subTest(message_length=len(message)):
                self.commit(message, (self.first, self.second))
                self.assert_blocked("git metadata:")

    def test_wrong_actual_parent_and_order_remain_blocked(self):
        for message in (
            f"Merge {self.first} into {self.second}",
            f"Merge {self.base} into {self.first}",
            f"Merge {self.second} into {self.base}",
            f"Merge {self.second.upper()} into {self.first}",
            f"Merge {'a' * 40} into {self.first}",
        ):
            with self.subTest(message=message[:6]):
                self.commit(message.encode() + b"\n", (self.first, self.second))
                self.assert_blocked("git metadata:")

    def test_zero_one_and_three_actual_parents_remain_blocked(self):
        for parents in ((), (self.first,), (self.first, self.second, self.base)):
            with self.subTest(parent_count=len(parents)):
                self.commit(self.canonical() + b"\n", parents)
                self.assert_blocked("git metadata:")

    def test_authors_and_committers_stay_strict_on_valid_synthetic_merge(self):
        for field in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            with self.subTest(field=field):
                self.commit(
                    self.canonical() + b"\n",
                    (self.first, self.second),
                    identity={field: "a" * 40},
                )
                self.assert_blocked("git authors:")

    def test_hex_tags_and_blocked_pattern_tags_remain_blocked(self):
        tag = "a" * 40
        self.git("update-ref", f"refs/tags/{tag}", self.base)
        self.assert_blocked("git tags:")
        self.git("update-ref", "-d", f"refs/tags/{tag}")
        tag = self.pattern().decode()
        self.git("check-ref-format", f"refs/tags/{tag}")
        self.git("update-ref", f"refs/tags/{tag}", self.base)
        self.assert_blocked("git tags: blocked pattern")

    def test_working_files_remain_strict(self):
        for payload in (b"a" * 64, self.pattern()):
            with self.subTest(length=len(payload)):
                (self.root / "payload.txt").write_bytes(payload)
                self.assert_blocked("payload.txt:")

    def test_clean_current_files_do_not_hide_history_leaks(self):
        for payload in (b"a" * 40, self.pattern()):
            with self.subTest(length=len(payload)):
                dirty = self.commit(b"old content\n", (self.base,), content=payload)
                self.commit(b"clean content\n", (dirty,))
                self.assertEqual((self.root / "content.txt").read_bytes(), b"safe\n")
                self.assert_blocked("git history")

    def test_other_refs_metadata_remains_scanned_with_valid_merge_at_head(self):
        dirty = self.commit(b"note " + b"a" * 64 + b"\n", (self.base,))
        self.git("update-ref", "refs/heads/other", dirty)
        self.commit(self.canonical() + b"\n", (self.first, self.second))
        self.assert_blocked("git metadata:")

    def test_blocked_pattern_metadata_is_not_exempted(self):
        for message in (
            b"note " + self.pattern() + b"\n",
            self.canonical() + b"\n\n" + self.pattern() + b"\n",
        ):
            with self.subTest(length=len(message)):
                self.commit(message, (self.first, self.second))
                self.assert_blocked("git metadata: blocked pattern")
