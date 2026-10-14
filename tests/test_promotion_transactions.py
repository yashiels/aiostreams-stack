import hashlib
import json
import subprocess
import unittest

import test_staged_promotion as fixtures

SCRIPT = fixtures.SCRIPT


class TransactionTests(unittest.TestCase):
    setUp = fixtures.PromotionTests.setUp
    run_command = fixtures.PromotionTests.run_command
    identity = fixtures.PromotionTests.identity
    arguments = fixtures.PromotionTests.arguments

    def prepare(self, kind="promotion"):
        deployment = (
            "#!/bin/bash\nset -eu\n"
            'python3 "$ADMISSION_SCRIPT" admit --production "$PWD"\n'
            'printf "%s\\n" "$0" >> "$EVENTS"\n'
        )
        for tree in (self.prod, self.stage):
            (tree / "ops/deploy.sh").write_text(deployment)
        if kind == "bootstrap":
            (self.prod / "ops/staged-promotion.py").unlink()
            (self.prod / "ops/with-lock.sh").write_text("old-lock")
            (self.prod / "ops/deploy.sh").write_text("old-deploy")
            return [
                "bootstrap",
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
                "--maintenance-window",
            ]
        return self.arguments()

    def operation(self, name):
        return [name, "--production", self.prod, "--transaction", self.transaction]

    def snapshot(self):
        result = {}
        for path in [self.prod, *self.prod.rglob("*")]:
            info = path.lstat()
            result[str(path.relative_to(self.prod))] = (
                info.st_mode,
                info.st_ino,
                info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        return result

    def fault(self, command, boundary, after=1, action=None):
        worker = self.root / "fault.py"
        consumed = self.root / "fault-consumed"
        consumed.unlink(missing_ok=True)
        worker.write_text(
            "import importlib.util, json, os, signal, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            'spec = importlib.util.spec_from_file_location("promotion", sys.argv[1])\n'
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "production, transaction = Path(sys.argv[2]), Path(sys.argv[3])\n"
            "boundary, after, action = sys.argv[4], int(sys.argv[5]), sys.argv[6]\n"
            "count = 0\n"
            "def consume(label):\n"
            "    global count\n"
            "    count += 1\n"
            "    if count == after:\n"
            '        with open(os.environ["CONSUMED"], "w") as stream:\n'
            "            stream.write(label); stream.flush(); os.fsync(stream.fileno())\n"
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            'if boundary in {"complete-write", "recovered-write", "checkpoint-write"}:\n'
            "    original = module.write_json\n"
            "    def fault(path, value):\n"
            "        result = original(path, value)\n"
            '        phase = "complete" if boundary == "complete-write" else "recovered"\n'
            '        selected = value.get("phase") == phase\n'
            '        if boundary == "checkpoint-write":\n'
            '            selected = value.get("phase") in {"promoting", "publishing", "recovering"} and value.get("index", 0) > 0 and value.get("pending") is None\n'
            '        if Path(path) == transaction / "state.json" and selected:\n'
            "            consume(boundary)\n"
            "        return result\n"
            '    target = "write_json"\n'
            'elif boundary == "file-rename-before":\n'
            "    original = module.os.replace\n"
            "    def fault(source, destination):\n"
            '        if Path(destination) == production / "versions.env": consume("file-rename-before")\n'
            "        return original(source, destination)\n"
            '    target = "os.replace"\n'
            'elif boundary == "marker-unlink":\n'
            "    original = module.durable_directory\n"
            "    def fault(path):\n"
            '        state_file = transaction / "state.json"\n'
            '        if Path(path) == production and not os.path.lexists(production / ".promotion-state") and state_file.exists():\n'
            '            if json.loads(state_file.read_text())["phase"] in {"complete", "recovered"}:\n'
            '                consume("marker-unlink")\n'
            "        return original(path)\n"
            '    target = "durable_directory"\n'
            "else:\n"
            "    original = module.apply_transition\n"
            "    def fault(source, root, operation):\n"
            '        selected = not action or operation["action"] == action\n'
            '        if selected and boundary == "transition-before": consume("transition-before")\n'
            "        result = original(source, root, operation)\n"
            '        if selected and boundary == "transition-after": consume("transition-after")\n'
            "        return result\n"
            '    target = "apply_transition"\n'
            'owner, attribute = (module.os, "replace") if target == "os.replace" else (module, target)\n'
            "with patch.object(owner, attribute, side_effect=fault):\n"
            "    module.execute(module.parser().parse_args(sys.argv[7:]))\n"
        )
        result = subprocess.run(
            [
                "python3",
                str(worker),
                str(SCRIPT),
                str(self.prod),
                str(self.transaction),
                boundary,
                str(after),
                action or "",
                *map(str, command),
            ],
            env=dict(self.env, CONSUMED=str(consumed)),
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, -9, result.stderr)
        self.assertTrue(consumed.exists(), result.stderr)
        self.assertEqual(consumed.read_text(), boundary)

    def assert_refusal_unchanged(self, command):
        before = self.snapshot()
        state_before = (self.transaction / "state.json").read_bytes()
        events = self.events.read_bytes() if self.events.exists() else None
        result = self.run_command(*command)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.transaction / "state.json").read_bytes(), state_before)
        self.assertEqual(
            self.events.read_bytes() if self.events.exists() else None, events
        )
        return result

    def test_complete_crashes_reconcile_without_replaying_services(self):
        for kind in ("bootstrap", "promotion"):
            for boundary in ("complete-write", "marker-unlink"):
                with self.subTest(kind=kind, boundary=boundary):
                    self.setUp()
                    self.fault(self.prepare(kind), boundary)
                    inode = (self.prod / ".stack.lock").stat().st_ino
                    baseline = self.identity(self.prod)
                    events = self.events.read_bytes() if self.events.exists() else None
                    files = {
                        str(path.relative_to(self.prod)): (
                            path.read_bytes(),
                            path.stat().st_mode,
                        )
                        for path in self.prod.rglob("*")
                        if path.is_file() and path.name != ".promotion-state"
                    }
                    for _ in range(2):
                        result = self.run_command(*self.operation("reconcile"))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(self.identity(self.prod), baseline)
                        self.assertEqual(
                            self.events.read_bytes() if self.events.exists() else None,
                            events,
                        )
                        self.assertFalse((self.prod / ".promotion-state").exists())
                    self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                    self.assertEqual(
                        files,
                        {
                            str(path.relative_to(self.prod)): (
                                path.read_bytes(),
                                path.stat().st_mode,
                            )
                            for path in self.prod.rglob("*")
                            if path.is_file()
                        },
                    )
                    module_path = self.root / "copy-stage.py"
                    module_path.write_text(
                        "import importlib.util, sys\nfrom pathlib import Path\n"
                        'spec = importlib.util.spec_from_file_location("promotion", sys.argv[1])\n'
                        "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                        "module.synchronize(Path(sys.argv[2]), Path(sys.argv[3]), module.inventory(Path(sys.argv[2])))\n"
                    )
                    subprocess.run(
                        [
                            "python3",
                            str(module_path),
                            str(SCRIPT),
                            str(self.prod),
                            str(self.stage),
                        ],
                        check=True,
                    )
                    self.transaction = self.root / "next-deployment"
                    args = self.arguments()
                    current_hash = hashlib.sha256(
                        (self.prod / "versions.env").read_bytes()
                    ).hexdigest()
                    args[args.index("--baseline-compose-sha") + 1] = current_hash
                    args[-1] = current_hash
                    result = self.run_command(*args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.identity(self.prod), baseline)
                    self.assertEqual(
                        (self.prod / "data/db").read_text(), "old-writer-data"
                    )

    def test_completed_tree_drift_refuses_reconciliation(self):
        for kind in ("bootstrap", "promotion"):
            with self.subTest(kind=kind):
                self.setUp()
                self.fault(self.prepare(kind), "complete-write")
                (self.prod / "versions.env").write_text("independent-later-edit")
                self.assert_refusal_unchanged(self.operation("reconcile"))
                self.assert_refusal_unchanged(self.operation("recover"))

    def test_interrupted_forward_and_recovery_refuse_independent_drift(self):
        for direction in ("forward", "recovery"):
            for change in (
                "edit",
                "add",
                "delete",
                "mode",
                "directory-add",
                "directory-delete",
                "directory-mode",
            ):
                with self.subTest(direction=direction, change=change):
                    self.setUp()
                    for tree in (self.prod, self.stage):
                        (tree / "untouched-directory").mkdir()
                    self.fault(self.prepare(), "transition-after", after=3)
                    if direction == "recovery":
                        self.fault(self.operation("recover"), "transition-after")
                    path = self.prod / "ops/compose-config-sha.sh"
                    if change == "edit":
                        path.write_text("independent-edit")
                    elif change == "add":
                        (self.prod / "independent-addition").write_text("later")
                    elif change == "delete":
                        path.unlink()
                    elif change == "mode":
                        path.chmod(0o600)
                    elif change == "directory-add":
                        (self.prod / "independent-directory").mkdir()
                    elif change == "directory-delete":
                        (self.prod / "untouched-directory").rmdir()
                    else:
                        (self.prod / "untouched-directory").chmod(0o700)
                    result = self.assert_refusal_unchanged(self.operation("recover"))
                    self.assertIn("live tree", result.stderr)
                    self.assertFalse(self.events.exists())

    def test_genuine_pending_before_and_after_transitions_restore_baseline(self):
        for boundary in ("transition-before", "transition-after"):
            with self.subTest(boundary=boundary):
                self.setUp()
                args = self.prepare()
                baseline = self.identity(self.prod)
                self.fault(args, boundary, after=2)
                inode = (self.prod / ".stack.lock").stat().st_ino
                self.fault(self.operation("recover"), boundary)
                for _ in range(2):
                    result = self.run_command(*self.operation("recover"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.identity(self.prod), baseline)
                self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                self.assertEqual((self.prod / ".env").read_text(), "synthetic-private")
                self.assertEqual((self.prod / "data/db").read_text(), "old-writer-data")
                self.assertFalse(self.events.exists())

    def test_reconciliation_interruption_is_retriable(self):
        for kind in ("bootstrap", "promotion"):
            with self.subTest(kind=kind):
                self.setUp()
                self.fault(self.prepare(kind), "complete-write")
                baseline = self.identity(self.prod)
                events = self.events.read_bytes() if self.events.exists() else None
                self.fault(self.operation("reconcile"), "marker-unlink")
                for _ in range(2):
                    result = self.run_command(*self.operation("reconcile"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.identity(self.prod), baseline)
                self.assertEqual(
                    self.events.read_bytes() if self.events.exists() else None, events
                )

    def test_recovered_terminal_crashes_are_retriable(self):
        for kind in ("bootstrap", "promotion"):
            for boundary in ("recovered-write", "marker-unlink"):
                with self.subTest(kind=kind, boundary=boundary):
                    self.setUp()
                    args = self.prepare(kind)
                    baseline = self.identity(self.prod)
                    self.fault(args, "transition-after")
                    self.fault(self.operation("recover"), boundary)
                    for _ in range(2):
                        result = self.run_command(*self.operation("recover"))
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.identity(self.prod), baseline)
                    self.assertFalse(self.events.exists())

    def test_atomic_file_rename_preserves_old_bytes_until_publication(self):
        args = self.prepare()
        baseline = self.identity(self.prod)
        version = (self.prod / "versions.env").stat()
        self.fault(args, "file-rename-before")
        self.assertEqual((self.prod / "versions.env").read_text(), "old")
        self.assertEqual((self.prod / "versions.env").stat().st_ino, version.st_ino)
        result = self.run_command(*self.operation("recover"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), baseline)
        self.assertFalse(self.events.exists())

    def test_directory_and_type_transitions_recover_from_real_faults(self):
        for direction in ("forward", "recovery"):
            for action in (
                "delete-directory",
                "create-directory",
                "chmod-directory",
                "publish-file",
            ):
                with self.subTest(direction=direction, action=action):
                    self.setUp()
                    (self.prod / "obsolete").mkdir()
                    (self.prod / "obsolete/child").write_text("old-child")
                    (self.stage / "introduced").mkdir()
                    (self.stage / "introduced/child").write_text("candidate-child")
                    (self.prod / "change").write_text("old-file")
                    (self.stage / "change").mkdir()
                    (self.stage / "change/child").write_text("candidate-file")
                    (self.prod / "flip").mkdir()
                    (self.prod / "flip/child").write_text("old-file")
                    (self.stage / "flip").write_text("candidate-file")
                    (self.prod / "modes").mkdir(mode=0o755)
                    (self.stage / "modes").mkdir(mode=0o700)
                    (self.stage / "config").chmod(0o700)
                    args = self.prepare()
                    baseline = self.identity(self.prod)
                    (self.prod / ".stack.lock").touch()
                    inode = (self.prod / ".stack.lock").stat().st_ino
                    if direction == "forward":
                        self.fault(args, "transition-after", action=action)
                    else:
                        self.fault(args, "transition-after", action="chmod-directory")
                        self.fault(
                            self.operation("recover"), "transition-after", action=action
                        )
                    result = self.run_command(*self.operation("recover"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.identity(self.prod), baseline)
                    self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                    self.assertEqual(
                        (self.prod / "config/household.json").read_text(),
                        "synthetic-private",
                    )
                    self.assertEqual(
                        (self.prod / "data/db").read_text(), "old-writer-data"
                    )
                    self.assertFalse(self.events.exists())

    def test_protected_descendants_are_never_replaced(self):
        (self.stage / "config").rmdir()
        args = self.prepare()
        baseline = self.identity(self.prod)
        result = self.run_command(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.prod / "config/household.json").read_text(), "synthetic-private"
        )
        result = self.run_command(*self.operation("recover"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), baseline)
        self.assertFalse(self.events.exists())

    def test_journal_schema_integrity_and_snapshots_checked_before_mutation(self):
        for damage in (
            "schema",
            "root",
            "journal",
            "old-snapshot",
            "candidate-snapshot",
            "foreign-marker",
        ):
            with self.subTest(damage=damage):
                self.setUp()
                self.fault(self.prepare(), "transition-after", after=2)
                if damage in ("schema", "root"):
                    path = self.transaction / "manifest.json"
                    value = json.loads(path.read_text())
                    if damage == "schema":
                        value["schema"] = 99
                    else:
                        value["production"] = str(self.stage)
                    path.write_text(json.dumps(value))
                elif damage == "journal":
                    path = self.transaction / "state.json"
                    value = json.loads(path.read_text())
                    value["index"] += 1
                    path.write_text(json.dumps(value))
                elif damage == "foreign-marker":
                    path = self.prod / ".promotion-state"
                    value = json.loads(path.read_text())
                    value["transaction"] = str(self.root / "foreign")
                    path.write_text(json.dumps(value))
                else:
                    directory = "old" if damage == "old-snapshot" else "candidate"
                    (self.transaction / directory / "versions.env").write_text(
                        "tampered"
                    )
                self.assert_refusal_unchanged(self.operation("recover"))
                self.assertFalse(self.events.exists())

    def test_reconciliation_requires_the_real_project_lock(self):
        import fcntl
        import time

        self.fault(self.prepare(), "complete-write")
        before = self.snapshot()
        events = self.events.read_bytes()
        with (self.prod / ".stack.lock").open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                ["python3", str(SCRIPT), *map(str, self.operation("reconcile"))],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertEqual(self.snapshot(), before)
                fcntl.flock(lock, fcntl.LOCK_UN)
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        self.assertEqual(self.events.read_bytes(), events)
        self.assertFalse((self.prod / ".promotion-state").exists())

    def test_committed_checkpoint_rejects_an_earlier_valid_tree(self):
        for direction in ("forward", "recovery"):
            with self.subTest(direction=direction):
                self.setUp()
                args = self.prepare()
                if direction == "forward":
                    historical = self.identity(self.prod)
                    self.fault(args, "checkpoint-write")
                    self.assertFalse((self.prod / "removed").exists())
                    (self.prod / "removed").write_text("removed")
                else:
                    self.fault(args, "transition-after", after=3)
                    historical = self.identity(self.prod)
                    self.fault(self.operation("recover"), "checkpoint-write")
                    self.assertFalse((self.prod / "added").exists())
                    (self.prod / "added").write_text("added")
                self.assertEqual(self.identity(self.prod), historical)
                result = self.assert_refusal_unchanged(self.operation("recover"))
                self.assertIn("live tree", result.stderr)
                self.assertFalse(self.events.exists())
