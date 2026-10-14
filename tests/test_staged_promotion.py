import hashlib
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/staged-promotion.py"
SCRATCH = Path(os.environ.get("M0_TEST_SCRATCH", tempfile.gettempdir())).resolve()


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRATCH.is_dir())
        self.root = Path(tempfile.mkdtemp(prefix="fixture-", dir=SCRATCH)).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.prod = self.root / "production"
        self.stage = self.root / "stage"
        for path in (self.prod, self.stage):
            (path / "ops").mkdir(parents=True)
            (path / "config").mkdir()
            (path / "versions.env").write_text("old")
            (path / "ops/compose-config-sha.sh").write_text(
                '#!/bin/bash\nsha256sum "$1/versions.env" | cut -d " " -f1\n'
            )
            (path / "ops/compose-config-sha.sh").chmod(0o755)
            (path / "ops/deploy.sh").write_text(
                "#!/bin/bash\nset -eu\n"
                'python3 "$ADMISSION_SCRIPT" admit --production "$PWD"\n'
                '[ "${FAIL_DEPLOY:-0}" = 0 ] || exit 1\n'
                'printf "deploy\\n" >> "$EVENTS"\n'
                'printf "backup-stop\\nbackup\\nbackup-start\\nup\\nprobe\\n" >> "$EVENTS"\n'
            )
            (path / "ops/deploy.sh").chmod(0o755)
            for name in ("ops/with-lock.sh", "ops/staged-promotion.py"):
                shutil.copy2(ROOT / name, path / name)
        (self.stage / "versions.env").write_text("new")
        (self.prod / "removed").write_text("removed")
        (self.stage / "added").write_text("added")
        (self.prod / ".env").write_text("synthetic-private")
        (self.prod / "config/household.json").write_text("synthetic-private")
        (self.prod / "config/configs.json").write_text("synthetic-private")
        (self.prod / "data").mkdir()
        (self.prod / "data/db").write_text("old-writer-data")
        (self.prod / "restore-rollback").mkdir()
        (self.prod / "restore-rollback/old").write_text("retained")
        self.events = self.root / "events"
        self.env = dict(
            os.environ,
            EVENTS=str(self.events),
            ADMISSION_SCRIPT=str(SCRIPT),
            MEDIA_GATEWAY_LOCK_HELD="1",
        )
        self.transaction = self.root / "transaction"

    def run_command(self, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), *map(str, args)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def identity(self, path):
        result = self.run_command("identity", "--root", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def arguments(self):
        return [
            "promote",
            "--production",
            self.prod,
            "--stage",
            self.stage,
            "--transaction",
            self.transaction,
            "--stage-sha",
            self.identity(self.stage),
            "--baseline-sha",
            self.identity(self.prod),
            "--baseline-compose-sha",
            hashlib.sha256(b"old").hexdigest(),
            "--candidate-compose-sha",
            hashlib.sha256(b"new").hexdigest(),
        ]

    def assert_refused(self, args):
        result = self.run_command(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.events.exists(), result.stdout)
        return result

    def test_success_preserves_private_files_and_lock_inode(self):
        args = self.arguments()
        (self.prod / ".stack.lock").touch()
        inode = (self.prod / ".stack.lock").stat().st_ino
        result = self.run_command(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
        self.assertEqual((self.prod / ".env").read_text(), "synthetic-private")
        for name in ("household.json", "configs.json"):
            self.assertEqual(
                (self.prod / "config" / name).read_text(), "synthetic-private"
            )
        self.assertEqual((self.prod / "data/db").read_text(), "old-writer-data")
        self.assertEqual((self.prod / "restore-rollback/old").read_text(), "retained")
        self.assertFalse((self.prod / "removed").exists())
        self.assertTrue((self.prod / "added").exists())
        self.assertEqual(
            self.events.read_text().splitlines(),
            ["deploy", "backup-stop", "backup", "backup-start", "up", "probe"],
        )

    def test_stale_and_tampered_inputs(self):
        for target in (self.prod, self.stage):
            with self.subTest(target=target.name):
                args = self.arguments()
                (target / "unexpected").write_text("drift")
                self.assert_refused(args)
                (target / "unexpected").unlink()
        args = self.arguments()
        args[args.index("--baseline-compose-sha") + 1] = "0" * 64
        self.assert_refused(args)

    def test_wrong_hash_requires_independent_recovery(self):
        args = self.arguments()
        old = self.identity(self.prod)
        args[-1] = "0" * 64
        self.assert_refused(args)
        self.assert_refused(self.arguments())
        result = self.run_command(
            "recover", "--production", self.prod, "--transaction", self.transaction
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), old)
        self.assertFalse(self.events.exists())

    def test_unsafe_paths_and_private_stage(self):
        args = self.arguments()
        (self.stage / ".env").write_text("private")
        self.assert_refused(args)
        (self.stage / ".env").unlink()
        (self.stage / "link").symlink_to(self.prod / "versions.env")
        self.assert_refused(args)
        (self.stage / "link").unlink()
        linked = self.root / "linked"
        linked.symlink_to(self.prod, target_is_directory=True)
        args[args.index("--production") + 1] = linked
        self.assert_refused(args)

    def test_partial_promotion_and_recovery(self):
        spec = importlib.util.spec_from_file_location("promotion", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = self.arguments()
        parsed = module.parser().parse_args(list(map(str, args)))
        from unittest.mock import patch

        real_transition = module.apply_transition
        calls = 0

        def fail_transition(source, destination, operation):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic partial promotion")
            return real_transition(source, destination, operation)

        old = self.identity(self.prod)
        with (
            patch.object(module, "apply_transition", side_effect=fail_transition),
            self.assertRaises(OSError),
        ):
            module.execute(parsed)
        self.assertEqual(calls, 2)
        self.assertFalse(self.events.exists())
        self.assert_refused(self.arguments())
        result = self.run_command(
            "recover", "--production", self.prod, "--transaction", self.transaction
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), old)

    def test_concurrency_six_contenders_five_repetitions(self):
        worker = self.root / "contender.py"
        worker.write_text(
            "import importlib.util, sys, time\n"
            "from pathlib import Path\n"
            'spec = importlib.util.spec_from_file_location("promotion", sys.argv[1])\n'
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "with module.locked(Path(sys.argv[2])):\n"
            '    with open(sys.argv[3], "a") as stream:\n'
            '        stream.write("enter\\n"); stream.flush()\n'
            "        time.sleep(0.03)\n"
            '        stream.write("leave\\n"); stream.flush()\n'
        )
        for repetition in range(5):
            log = self.root / f"contention-{repetition}"
            contenders = [
                subprocess.Popen(
                    ["python3", str(worker), str(SCRIPT), str(self.prod), str(log)],
                    env=self.env,
                )
                for _ in range(6)
            ]
            self.addCleanup(
                lambda contenders=contenders: [
                    process.kill() for process in contenders if process.poll() is None
                ]
            )
            for process in contenders:
                self.assertEqual(process.wait(timeout=15), 0)
            active = peak = 0
            lines = log.read_text().splitlines()
            for line in lines:
                active += 1 if line == "enter" else -1
                peak = max(active, peak)
                self.assertIn(active, (0, 1))
            self.assertEqual((peak, active, len(lines)), (1, 0, 12))

    def test_foreign_held_marker_cannot_bypass_lock(self):
        import fcntl
        import time

        args = self.arguments()
        with (self.prod / ".stack.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                ["python3", str(SCRIPT), *map(str, args)],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse(self.transaction.exists())
                self.assertFalse(self.events.exists())
                fcntl.flock(lock, fcntl.LOCK_UN)
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_killed_promotion_and_interrupted_recovery(self):
        args = self.arguments()
        old = self.identity(self.prod)
        worker = self.root / "interrupt.py"
        worker.write_text(
            "import importlib.util, os, signal, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            'spec = importlib.util.spec_from_file_location("promotion", sys.argv[1])\n'
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "original = module.apply_transition\n"
            "def interrupt(source, destination, operation):\n"
            "    result = original(source, destination, operation)\n"
            "    if Path(destination).is_relative_to(Path(sys.argv[2])):\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "    return result\n"
            'with patch.object(module, "apply_transition", side_effect=interrupt):\n'
            "    module.execute(module.parser().parse_args(sys.argv[3:]))\n"
        )
        recovery = [
            "recover",
            "--production",
            self.prod,
            "--transaction",
            self.transaction,
        ]
        for command in (args, recovery):
            result = subprocess.run(
                [
                    "python3",
                    str(worker),
                    str(SCRIPT),
                    str(self.prod),
                    *map(str, command),
                ],
                env=self.env,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, -9, result.stderr)
            self.assertFalse(self.events.exists())
            self.assert_refused(self.arguments())
        result = self.run_command(*recovery)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), old)

    def test_existing_deploy_and_backup_nested_order(self):
        import json

        for tree in (self.stage, self.prod):
            (tree / "backup").mkdir()
            for name in (
                "ops/deploy.sh",
                "ops/with-lock.sh",
                "ops/compose-config-sha.sh",
                "ops/staged-promotion.py",
                "backup/backup.sh",
            ):
                shutil.copy2(ROOT / name, tree / name)
            exporter = tree / "ops/env-export.py"
            exporter.write_text(
                "#!/usr/bin/env python3\nprint("
                + repr(
                    f"MEDIA_UID={os.getuid()}; MEDIA_GID={os.getgid()}; "
                    "RESTIC_REPOSITORY=synthetic; RESTIC_PASSWORD=synthetic"
                )
                + ")\n"
            )
            exporter.chmod(0o755)
            for name in ("ops/env-check.py", "ops/install-timer.sh", "ops/probe.sh"):
                path = tree / name
                path.write_text(
                    '#!/bin/bash\nprintf "%s\\n" '
                    + json.dumps(name)
                    + ' >> "$EVENTS"\n'
                )
                path.chmod(0o755)
        (self.prod / "data/aiostreams").mkdir()
        (self.prod / "data/aiostreams/db.sqlite").write_text("old-writer-data")
        binary = self.root / "bin"
        binary.mkdir()
        commands = {
            "df": '#!/bin/bash\nprintf "Available\\n100G\\n"\n',
            "restic": '#!/bin/bash\nprintf "restic %s\\n" "$*" >> "$EVENTS"\n'
            'case "$*" in *--json*) printf \'[{"time":"synthetic"}]\\n\';; esac\n',
            "docker": "#!/bin/bash\nset -eu\n"
            'case "$*" in\n'
            '  *" config") cat versions.env;;\n'
            '  "inspect "*) echo healthy;;\n'
            '  *"ps --status running"*) echo aiostreams;;\n'
            '  *"ps -q"*) echo synthetic-container;;\n'
            '  *) printf "docker %s\\n" "$*" >> "$EVENTS";;\n'
            "esac\n",
        }
        for name, content in commands.items():
            path = binary / name
            path.write_text(content)
            path.chmod(0o755)
        self.env["PATH"] = str(binary) + os.pathsep + self.env["PATH"]
        result = self.run_command(*self.arguments())
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.events.read_text().splitlines()
        stop = next(
            i for i, line in enumerate(lines) if line.endswith("stop aiostreams")
        )
        backup = next(
            i for i, line in enumerate(lines) if line.startswith("restic backup")
        )
        start = next(
            i for i, line in enumerate(lines) if line.endswith("start aiostreams")
        )
        up = next(i for i, line in enumerate(lines) if line.endswith("up -d"))
        probe = lines.index("ops/probe.sh")
        self.assertLess(stop, backup)
        self.assertLess(backup, start)
        self.assertLess(start, up)
        self.assertLess(up, probe)

    def test_admission_refuses_foreign_descriptor_and_unlocked_marker(self):
        self.env["FAIL_DEPLOY"] = "1"
        self.assert_refused(self.arguments())
        self.env["MEDIA_GATEWAY_PROMOTION"] = str(self.transaction)
        for path in (self.root / "foreign.lock", self.prod / ".stack.lock"):
            with path.open("w") as descriptor:
                environment = dict(
                    self.env, MEDIA_GATEWAY_PROMOTION_FD=str(descriptor.fileno())
                )
                result = subprocess.run(
                    ["python3", str(SCRIPT), "admit", "--production", str(self.prod)],
                    env=environment,
                    pass_fds=(descriptor.fileno(),),
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
        self.assert_refused(
            ["recover", "--production", self.prod, "--transaction", self.transaction]
        )

    def test_foreign_lock_and_overlapping_roots_refused(self):
        args = self.arguments()
        self.env["MEDIA_GATEWAY_LOCKFILE"] = str(self.root / "foreign.lock")
        self.assert_refused(args)
        self.env.pop("MEDIA_GATEWAY_LOCKFILE")
        args[args.index("--transaction") + 1] = self.prod / "transaction"
        self.assert_refused(args)
        args = self.arguments()
        args[args.index("--stage") + 1] = self.prod
        self.assert_refused(args)

    def test_failed_deployment_never_resumes_or_restores_old_pin(self):
        self.env["FAIL_DEPLOY"] = "1"
        self.assert_refused(self.arguments())
        self.assertEqual((self.prod / "versions.env").read_text(), "new")
        self.assert_refused(self.arguments())
        self.assert_refused(
            ["recover", "--production", self.prod, "--transaction", self.transaction]
        )
        self.assertEqual((self.prod / "versions.env").read_text(), "new")
        self.assertEqual((self.prod / "data/db").read_text(), "old-writer-data")

    def test_recovery_snapshot_tampering_refused(self):
        args = self.arguments()
        args[-1] = "0" * 64
        self.assert_refused(args)
        (self.transaction / "old/versions.env").write_text("tampered")
        self.assert_refused(
            ["recover", "--production", self.prod, "--transaction", self.transaction]
        )
        self.assertEqual((self.prod / "versions.env").read_text(), "new")

    def test_nested_exclusions_hardlinks_and_broken_marker(self):
        nested = self.prod / "nested"
        nested.mkdir()
        (nested / ".env").write_text("synthetic-private")
        (self.stage / "nested").mkdir()
        result = self.run_command(*self.arguments())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((nested / ".env").read_text(), "synthetic-private")
        self.events.unlink()
        (self.stage / "hardlink").hardlink_to(self.stage / "versions.env")
        self.assert_refused(["identity", "--root", self.stage])
        (self.stage / "hardlink").unlink()
        (self.prod / ".promotion-state").symlink_to(self.root / "missing")
        self.assert_refused(self.arguments())
        self.assert_refused(["admit", "--production", self.prod])

    def test_promotion_guards_cannot_change(self):
        args = self.arguments()
        for name in ("ops/deploy.sh", "ops/with-lock.sh", "ops/staged-promotion.py"):
            with self.subTest(name=name):
                path = self.stage / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assert_refused(self.arguments())
                self.assertFalse(self.transaction.exists())
                path.write_bytes(original)
        self.assertEqual(args, self.arguments())
