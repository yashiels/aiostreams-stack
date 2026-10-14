import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/staged-promotion.py"
GUARDS = ("ops/with-lock.sh", "ops/staged-promotion.py", "ops/deploy.sh")
SCRATCH = Path(os.environ.get("M0_TEST_SCRATCH", tempfile.gettempdir())).resolve()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=SCRATCH)).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.prod = self.root / "production"
        self.stage = self.root / "stage"
        for tree in (self.prod, self.stage):
            (tree / "ops").mkdir(parents=True)
            (tree / "versions.env").write_text("old-image")
            (tree / "compose.yml").write_text(
                "volumes: [./data/aiostreams:/app/data]\n"
            )
        for name in GUARDS:
            shutil.copy2(ROOT / name, self.stage / name)
        for name in ("ops/with-lock.sh", "ops/deploy.sh"):
            (self.prod / name).write_text('#!/bin/bash\necho service >> "$EVENTS"\n')
            (self.prod / name).chmod(0o755)
        (self.prod / ".env").write_text("PRIVATE_VALUE=synthetic-private\n")
        (self.prod / ".env").chmod(0o600)
        (self.prod / "data").mkdir()
        (self.prod / "data/old").write_text("old-data")
        self.transaction = self.root / "transaction"
        self.events = self.root / "events"
        self.env = dict(
            os.environ, EVENTS=str(self.events), MEDIA_GATEWAY_LOCK_HELD="1"
        )

    def run_command(self, *args):
        return subprocess.run(
            ["python3", str(SCRIPT), *map(str, args)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def identity(self, tree):
        result = self.run_command("identity", "--root", tree)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def arguments(self):
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

    def recovery(self):
        return ["recover", "--production", self.prod, "--transaction", self.transaction]

    def assert_refused(self, args):
        result = self.run_command(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.events.exists())
        return result

    def test_bootstrap_missing_old_helper_and_complete_or_absent_sets(self):
        for initial in ("partial", "absent", "complete"):
            with self.subTest(initial=initial):
                if initial == "absent":
                    for name in GUARDS:
                        (self.prod / name).unlink(missing_ok=True)
                elif initial == "complete":
                    for name in GUARDS:
                        shutil.copy2(ROOT / name, self.prod / name)
                self.transaction = self.root / initial
                (self.prod / ".stack.lock").touch()
                inode = (self.prod / ".stack.lock").stat().st_ino
                result = self.run_command(*self.arguments())
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in GUARDS:
                    self.assertEqual(
                        (self.prod / name).read_bytes(),
                        (self.stage / name).read_bytes(),
                    )
                self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                self.assertEqual(
                    (self.prod / ".env").read_text(),
                    "PRIVATE_VALUE=synthetic-private\n",
                )
                self.assertEqual((self.prod / "data/old").read_text(), "old-data")
                self.assertFalse(self.events.exists())
                self.assertFalse((self.prod / ".promotion-state").exists())
                self.assert_refused(self.recovery())

    def test_bootstrap_stale_tampered_foreign_and_missing_window(self):
        args = self.arguments()
        self.assert_refused(args[:-1])
        (self.prod / "later-edit").write_text("later")
        self.assert_refused(args)
        (self.prod / "later-edit").unlink()
        (self.stage / "ops/deploy.sh").write_text("tampered")
        self.assert_refused(args)
        shutil.copy2(ROOT / "ops/deploy.sh", self.stage / "ops/deploy.sh")
        self.env["MEDIA_GATEWAY_LOCKFILE"] = str(self.root / "foreign.lock")
        self.assert_refused(self.arguments())
        self.assertFalse(self.transaction.exists())

    def test_bootstrap_real_lock_contention_ignores_foreign_held_marker(self):
        import fcntl
        import time

        args = self.arguments()
        with (self.prod / ".stack.lock").open("w") as lock:
            inode = (self.prod / ".stack.lock").stat().st_ino
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
                fcntl.flock(lock, fcntl.LOCK_UN)
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                self.assertFalse(self.events.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def interrupt(self, command, after=1):
        worker = self.root / "interrupt.py"
        worker.write_text(
            "import importlib.util, os, signal, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            'spec = importlib.util.spec_from_file_location("promotion", sys.argv[1])\n'
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "original = module.publish_guard\n"
            "count = 0\n"
            "def interrupt(*args, **kwargs):\n"
            "    global count\n"
            "    result = original(*args, **kwargs)\n"
            "    count += 1\n"
            '    if count == int(os.environ["KILL_AFTER"]):\n'
            "        os.kill(os.getpid(), signal.SIGKILL)\n"
            "    return result\n"
            'with patch.object(module, "publish_guard", side_effect=interrupt):\n'
            "    module.execute(module.parser().parse_args(sys.argv[2:]))\n"
        )
        return subprocess.run(
            ["python3", str(worker), str(SCRIPT), *map(str, command)],
            env=dict(self.env, KILL_AFTER=str(after)),
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_bootstrap_publication_and_recovery_interruption(self):
        baseline = self.identity(self.prod)
        for after in (1, 2, 3):
            self.transaction = self.root / f"interrupted-{after}"
            result = self.interrupt(self.arguments(), after=after)
            self.assertEqual(result.returncode, -9, result.stderr)
            self.assertTrue((self.prod / ".promotion-state").exists())
            self.assertFalse(self.events.exists())
            self.assert_refused(self.arguments())
            self.assert_refused(["admit", "--production", self.prod])
            result = self.interrupt(self.recovery(), after=2)
            self.assertEqual(result.returncode, -9, result.stderr)
            self.assertTrue((self.prod / ".promotion-state").exists())
            self.assertFalse(self.events.exists())
            result = self.run_command(*self.recovery())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.identity(self.prod), baseline)
            self.assertFalse((self.prod / "ops/staged-promotion.py").exists())

    def test_bootstrap_recovery_refuses_later_edits_and_snapshot_tamper(self):
        result = self.interrupt(self.arguments())
        self.assertEqual(result.returncode, -9, result.stderr)
        guard = self.prod / "ops/with-lock.sh"
        original = guard.read_bytes()
        guard.write_bytes(b"later-edit")
        self.assert_refused(self.recovery())
        self.assertEqual(guard.read_bytes(), b"later-edit")
        guard.write_bytes(original)
        (self.prod / "versions.env").write_text("later-image")
        self.assert_refused(self.recovery())
        (self.prod / "versions.env").write_text("old-image")
        (self.transaction / "old/ops/deploy.sh").write_text("tampered")
        self.assert_refused(self.recovery())

    def documented_blocks(self):
        section = (
            (ROOT / "RUNBOOK.md").read_text().split("## Locked staged promotion", 1)[1]
        )
        section = section.split("Add `--no-tunnel`", 1)[0]
        blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
        replacements = {
            "/srv/reviewed-stage": str(self.stage),
            "/srv/stack-transactions": str(self.root / "transactions"),
            "/srv/stack": str(self.prod),
            "/srv/private-renders": str(self.root / "private-renders"),
        }
        for old, new in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            blocks = [block.replace(old, new) for block in blocks]
        return blocks

    def prepare_documented_workflow(self):
        for tree in (self.prod, self.stage):
            shutil.copy2(
                ROOT / "ops/compose-config-sha.sh", tree / "ops/compose-config-sha.sh"
            )
            exporter = tree / "ops/env-export.py"
            exporter.write_text('#!/usr/bin/env python3\nprint("")\n')
            exporter.chmod(0o755)
        deploy = self.stage / "ops/deploy.sh"
        deploy.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            '. ./ops/with-lock.sh\ntake_lock "$@"\n'
            'python3 ./ops/staged-promotion.py admit --production "$PWD"\n'
            'test "$(bash ./ops/compose-config-sha.sh "$PWD")" = "$EXPECTED_COMPOSE_SHA256"\n'
            'printf "%s\\n" "$0" >> "$DEPLOY_EVENTS"\n'
        )
        (self.stage / "versions.env").write_text("candidate-image")
        private = self.root / "private-renders/upgrade"
        (self.prod / ".env").write_text(
            f"PRIVATE_VALUE=synthetic-private\nPRIVATE_PATH={private}\n"
        )
        secret = (self.prod / ".env").read_bytes()
        self.env["DEPLOY_EVENTS"] = str(self.root / "deploy-events")
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            'with open(os.environ["EVENTS"], "a") as log:\n'
            '    log.write(json.dumps(args) + "\\n")\n'
            'if args[-1] != "config": raise SystemExit(19)\n'
            'envs = [Path(args[i+1]) for i, arg in enumerate(args) if arg == "--env-file"]\n'
            'project = Path(args[args.index("--project-directory")+1]) '
            'if "--project-directory" in args else Path.cwd()\n'
            'model = {"image": envs[1].read_text(), "secret": envs[0].read_text(), '
            '"mount": str(project / "data/aiostreams")}\n'
            "print(json.dumps(model, sort_keys=True))\n"
        )
        docker.chmod(0o755)
        self.env["PATH"] = str(binary) + os.pathsep + self.env["PATH"]
        return private, secret

    def run_documented_phase(self, block, phase, variables, hashes=()):
        script = block
        if hashes:
            script += '\nprintf "%s\\n" ' + " ".join(
                f'"${{{name}}}"' for name in hashes
            )
        (self.root / f"{phase}.command").write_text(script)
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=dict(self.env, **variables),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        for suffix, value in (
            ("stdout", result.stdout),
            ("stderr", result.stderr),
            ("status", str(result.returncode)),
        ):
            (self.root / f"{phase}.{suffix}").write_text(value)
        with (self.root / "workflow-order").open("a") as stream:
            stream.write(phase + "\n")
        self.assertNotIn("synthetic-private", result.stdout + result.stderr)
        if result.returncode == 0 and hashes:
            values = result.stdout.splitlines()
            self.assertEqual(len(values), len(hashes))
            variables.update(zip(hashes, values, strict=True))
        return result

    def workflow_variables(self):
        return {
            "REVIEWED": str(self.stage),
            "PRODUCTION": str(self.prod),
            "TRANSACTIONS": str(self.root / "transactions"),
            "PRIVATE_RENDER": str(self.root / "private-renders/upgrade"),
        }

    def assert_bootstrap_block(self, block):
        self.assertIn('python3 "$REVIEWED/ops/staged-promotion.py" bootstrap', block)
        self.assertIn(
            'cmp "$REVIEWED/ops/$guard" "$PRODUCTION/ops/$guard" || exit 1', block
        )
        loop = re.search(r"for guard in ([^;]+); do", block)
        self.assertIsNotNone(loop)
        self.assertEqual(loop.group(1).split(), [Path(name).name for name in GUARDS])

    def test_documented_private_render_and_promotion_workflow(self):
        private, secret = self.prepare_documented_workflow()
        stage_sha = self.identity(self.stage)
        blocks = self.documented_blocks()
        self.assertEqual(len(blocks), 5)
        self.assert_bootstrap_block(blocks[1])
        variables = self.workflow_variables()
        result = self.run_documented_phase(
            blocks[0],
            "capture",
            variables,
            ("BOOTSTRAP_STAGE_SHA", "BOOTSTRAP_OLD_TREE_SHA"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_documented_phase(blocks[1], "bootstrap", variables)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in GUARDS:
            self.assertEqual(
                (self.prod / name).read_bytes(), (self.stage / name).read_bytes()
            )
            self.assertEqual(
                (self.prod / name).stat().st_mode, (self.stage / name).stat().st_mode
            )
        self.assertFalse(os.path.lexists(self.prod / ".promotion-state"))
        self.assertFalse(self.events.exists())
        self.assertFalse((self.root / "deploy-events").exists())
        bootstrap_state = json.loads(
            (self.root / "transactions/bootstrap/state.json").read_text()
        )
        self.assertEqual(bootstrap_state["phase"], "complete")
        observations = {
            "guards": {
                name: {
                    "production_sha": hashlib.sha256(
                        (self.prod / name).read_bytes()
                    ).hexdigest(),
                    "candidate_sha": hashlib.sha256(
                        (self.stage / name).read_bytes()
                    ).hexdigest(),
                    "production_mode": (self.prod / name).stat().st_mode,
                    "candidate_mode": (self.stage / name).stat().st_mode,
                }
                for name in GUARDS
            },
            "marker_present": os.path.lexists(self.prod / ".promotion-state"),
            "service_log_present": self.events.exists(),
            "deploy_log_present": (self.root / "deploy-events").exists(),
        }
        (self.root / "bootstrap-observations.json").write_text(json.dumps(observations))
        result = self.run_documented_phase(
            blocks[3],
            "render",
            variables,
            ("OLD_TREE_SHA", "OLD_COMPOSE_SHA", "STAGE_SHA", "CANDIDATE_COMPOSE_SHA"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate_render = variables["CANDIDATE_COMPOSE_SHA"]
        self.assertEqual(len(candidate_render), len(hashlib.sha256(b"").hexdigest()))
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((private / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((private / ".env").read_bytes(), secret)
        self.assertFalse((private / "data").exists())
        self.assertFalse((self.stage / ".env").exists())
        self.assertEqual(self.identity(self.prod), variables["OLD_TREE_SHA"])
        self.assertEqual(self.identity(self.stage), stage_sha)
        self.assertEqual(
            {path.relative_to(private) for path in private.rglob("*")},
            {path.relative_to(self.stage) for path in self.stage.rglob("*")}
            | {Path(".env")},
        )
        for name in GUARDS:
            self.assertEqual(
                (private / name).read_bytes(), (self.prod / name).read_bytes()
            )
            self.assertEqual(
                (private / name).stat().st_mode, (self.prod / name).stat().st_mode
            )
        before_refusal = self.events.read_bytes()
        private_refusal = (
            blocks[4]
            .split("\ntest ", 1)[0]
            .replace('--stage "$REVIEWED"', '--stage "$PRIVATE_RENDER"')
        )
        result = self.run_documented_phase(
            private_refusal, "private-refusal", variables
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("stage contains a protected path", result.stderr)
        self.assertNotIn("install and retain", result.stderr)
        self.assertNotIn("baseline mismatch", result.stderr)
        self.assertEqual((self.root / "private-refusal.status").read_text(), "1")
        self.assertEqual(
            (self.root / "private-refusal.stdout").read_text(), result.stdout
        )
        self.assertEqual(
            (self.root / "private-refusal.stderr").read_text(), result.stderr
        )
        self.assertEqual(self.events.read_bytes(), before_refusal)
        self.assertFalse((self.root / "deploy-events").exists())
        self.assertFalse((self.root / "transactions/upgrade").exists())
        self.assertFalse(os.path.lexists(self.prod / ".promotion-state"))
        self.assertEqual(self.identity(self.stage), stage_sha)
        result = self.run_documented_phase(blocks[4], "promote", variables)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = subprocess.run(
            ["bash", str(self.prod / "ops/compose-config-sha.sh"), str(self.prod)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(candidate_render, actual)
        self.assertEqual(
            (self.root / "deploy-events").read_text().splitlines(),
            [str(self.prod / "ops/deploy.sh")],
        )
        self.assertEqual((self.prod / "versions.env").read_text(), "candidate-image")
        self.assertEqual((self.prod / ".env").read_bytes(), secret)
        self.assertEqual((self.prod / "data/old").read_text(), "old-data")
        self.assertEqual(self.identity(self.stage), stage_sha)
        calls = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertTrue(
            any(
                "--project-directory" in call and str(self.prod) in call
                for call in calls
            )
        )
        self.assertTrue(all(call[-1] == "config" for call in calls))
        self.assertEqual(
            (self.root / "workflow-order").read_text().splitlines(),
            ["capture", "bootstrap", "render", "private-refusal", "promote"],
        )

    def test_documented_workflow_without_bootstrap_refuses_guard_mismatch(self):
        self.prepare_documented_workflow()
        stage_sha = self.identity(self.stage)
        blocks = self.documented_blocks()
        self.assert_bootstrap_block(blocks[1])
        variables = self.workflow_variables()
        result = self.run_documented_phase(
            blocks[0],
            "capture",
            variables,
            ("BOOTSTRAP_STAGE_SHA", "BOOTSTRAP_OLD_TREE_SHA"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_documented_phase(
            blocks[3],
            "render",
            variables,
            ("OLD_TREE_SHA", "OLD_COMPOSE_SHA", "STAGE_SHA", "CANDIDATE_COMPOSE_SHA"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        before_refusal = self.events.read_bytes()
        result = self.run_documented_phase(
            blocks[4], "missing-bootstrap-refusal", variables
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "install and retain the reviewed promotion guards before staging",
            result.stderr,
        )
        self.assertNotIn("stage contains a protected path", result.stderr)
        self.assertEqual(self.events.read_bytes(), before_refusal)
        self.assertFalse((self.root / "deploy-events").exists())
        self.assertFalse((self.root / "transactions/bootstrap").exists())
        self.assertFalse((self.root / "transactions/upgrade").exists())
        self.assertFalse((self.prod / "ops/staged-promotion.py").exists())
        self.assertFalse(os.path.lexists(self.prod / ".promotion-state"))
        self.assertNotEqual(
            (self.prod / "ops/deploy.sh").read_bytes(),
            (self.stage / "ops/deploy.sh").read_bytes(),
        )
        self.assertEqual(self.identity(self.prod), variables["OLD_TREE_SHA"])
        self.assertEqual(self.identity(self.stage), stage_sha)
        self.assertEqual((self.prod / "versions.env").read_text(), "old-image")
        actual = subprocess.run(
            ["bash", str(self.prod / "ops/compose-config-sha.sh"), str(self.prod)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertNotEqual(actual, variables["CANDIDATE_COMPOSE_SHA"])
        self.assertEqual(
            (self.root / "workflow-order").read_text().splitlines(),
            ["capture", "render", "missing-bootstrap-refusal"],
        )

    def test_documented_bootstrap_recovery_and_second_bootstrap_stale_baseline(self):
        self.transaction = self.root / "bootstrap"
        args = self.arguments()
        baseline = self.identity(self.prod)
        result = self.interrupt(args, after=2)
        self.assertEqual(result.returncode, -9, result.stderr)
        environment = dict(
            self.env,
            REVIEWED=str(self.stage),
            PRODUCTION=str(self.prod),
            TRANSACTIONS=str(self.root),
        )
        block = self.documented_blocks()[2]
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.identity(self.prod), baseline)
        args[args.index("--transaction") + 1] = self.root / "fresh-bootstrap"
        result = self.run_command(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.prod / "versions.env").write_text("unrelated-later-edit")
        args[args.index("--transaction") + 1] = self.root / "second-bootstrap"
        self.assert_refused(args)
        self.assertFalse((self.root / "second-bootstrap").exists())
        self.assertEqual(
            (self.prod / "versions.env").read_text(), "unrelated-later-edit"
        )

    def test_bootstrap_recovery_contends_on_same_lock(self):
        import fcntl
        import time

        result = self.interrupt(self.arguments(), after=2)
        self.assertEqual(result.returncode, -9, result.stderr)
        with (self.prod / ".stack.lock").open("r") as lock:
            inode = (self.prod / ".stack.lock").stat().st_ino
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                ["python3", str(SCRIPT), *map(str, self.recovery())],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertTrue((self.prod / "ops/staged-promotion.py").exists())
                self.assertTrue((self.prod / ".promotion-state").exists())
                fcntl.flock(lock, fcntl.LOCK_UN)
                _, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual((self.prod / ".stack.lock").stat().st_ino, inode)
                self.assertFalse((self.prod / "ops/staged-promotion.py").exists())
                self.assertFalse(self.events.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_compose_sha_rejects_argument_counts_with_exact_usage(self):
        helper = ROOT / "ops/compose-config-sha.sh"
        usage = "usage: compose-config-sha.sh <stack-dir> [project-directory]\n"
        self.assertFalse((self.root / ".env").exists())
        for arguments in (
            (),
            (self.root, self.root, "extra"),
            (self.root, self.root, "extra", "another"),
        ):
            with self.subTest(count=len(arguments)):
                result = subprocess.run(
                    ["bash", str(helper), *map(str, arguments)],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, usage)
                self.assertFalse(self.events.exists())
