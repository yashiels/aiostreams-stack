#!/usr/bin/env python3
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

GUARDS = ("ops/with-lock.sh", "ops/staged-promotion.py", "ops/deploy.sh")

PROTECTED = {
    ".env",
    ".git",
    ".stack.lock",
    ".promotion-state",
    ".promotion-state.new",
    "data",
    "restore-rollback",
    "config/household.json",
    "config/configs.json",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    path = Path(value)
    require(
        path.is_absolute() and path == path.resolve(), "use an absolute canonical path"
    )
    require(path != Path("/") and path.parent.is_dir(), "unsafe root")
    return path


def excluded(relative):
    parts = relative.parts
    return any(
        "/".join(parts[:i]) in PROTECTED for i in range(1, len(parts) + 1)
    ) or any(
        part
        in {".env", ".git", ".stack.lock", "data", "restore-rollback", "__pycache__"}
        or part.startswith(("restore.", ".promotion-state"))
        for part in parts
    )


def inventory(root, stage=False):
    require(root.is_dir(), "missing tree")
    result = {}

    def walk(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root)
            if excluded(relative):
                require(not stage, "stage contains a protected path")
                continue
            info = path.lstat()
            require(
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                "tree contains a symlink or special file",
            )
            mode = stat.S_IMODE(info.st_mode)
            require(not mode & 0o7000, "unsafe file permissions")
            if path.is_dir():
                result[str(relative)] = ["directory", mode]
                walk(path)
            else:
                require(info.st_nlink == 1, "tree contains a hard link")
                result[str(relative)] = [
                    "file",
                    mode,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ]

    walk(root)
    return result


def digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def durable_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        durable_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(production):
    lock = production / ".stack.lock"
    configured = os.environ.get("MEDIA_GATEWAY_LOCKFILE")
    require(not configured or Path(configured) == lock, "foreign project lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(
            stat.S_ISREG(os.fstat(descriptor).st_mode)
            and os.fstat(descriptor).st_nlink == 1,
            "unsafe lock file",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        require(
            os.stat(lock, follow_symlinks=False).st_ino == os.fstat(descriptor).st_ino,
            "lock identity changed",
        )
        yield descriptor
    finally:
        os.close(descriptor)


def compose_sha(production, project_directory=None):
    command = ["bash", str(production / "ops/compose-config-sha.sh"), str(production)]
    if project_directory is not None:
        command.append(str(project_directory))
    result = subprocess.run(
        command,
        cwd=production,
        capture_output=True,
        check=True,
    )
    value = result.stdout.decode().strip()
    require(
        len(value) == 64 and all(c in "0123456789abcdef" for c in value),
        "invalid rendered hash",
    )
    return value


def synchronize(source, destination, entries):
    current = inventory(destination)
    for name in sorted(current, key=lambda item: (item.count("/"), item), reverse=True):
        if name not in entries or entries[name][0] != current[name][0]:
            path = destination / name
            if current[name][0] == "file":
                path.unlink()
            else:
                try:
                    path.rmdir()
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise
                    require(
                        name not in entries or entries[name][0] == "directory",
                        "cannot replace a protected parent directory",
                    )
    for name, entry in sorted(
        entries.items(), key=lambda item: (item[0].count("/"), item[0])
    ):
        path = destination / name
        if entry[0] == "directory":
            path.mkdir(exist_ok=True)
        else:
            require(not path.is_dir(), "cannot replace a protected parent directory")
            if current.get(name) == entry:
                continue
            shutil.copy2(source / name, path)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        os.chmod(path, entry[1])
    for directory in sorted(
        [destination]
        + [
            destination / name
            for name, entry in entries.items()
            if entry[0] == "directory"
        ],
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        durable_directory(directory)
    require(inventory(destination) == entries, "file verification failed")


def transaction_path(value, production, stage=None):
    transaction = canonical(value)
    for root in (production, stage):
        if root is not None:
            require(
                not transaction.is_relative_to(root)
                and not root.is_relative_to(transaction),
                "transaction must be outside managed trees",
            )
    parent = transaction.parent.stat()
    require(
        parent.st_uid == os.getuid() and not stat.S_IMODE(parent.st_mode) & 0o077,
        "transaction parent must be owner-only",
    )
    return transaction


def guard_inventory(entries):
    return {name: entries[name] for name in ("ops", *GUARDS) if name in entries}


def publish_guard(source, production, name, entry):
    destination = production / name
    if entry is None:
        destination.unlink(missing_ok=True)
        durable_directory(destination.parent)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".promotion-state.guard.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((source / name).read_bytes())
            stream.flush()
            shutil.copystat(source / name, temporary)
            os.fchmod(stream.fileno(), entry[1])
            os.fsync(stream.fileno())
        require(
            hashlib.sha256(temporary.read_bytes()).hexdigest() == entry[2],
            "guard snapshot changed during publication",
        )
        os.replace(temporary, destination)
        durable_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def validate_entries(entries):
    require(isinstance(entries, dict), "invalid transaction entries")
    for name, entry in entries.items():
        require(
            isinstance(name, str)
            and name
            and Path(name) != Path(".")
            and str(Path(name)) == name
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and not excluded(Path(name)),
            "invalid transaction path",
        )
        require(
            isinstance(entry, list) and len(entry) in {2, 3},
            "invalid transaction entry",
        )
        require(
            type(entry[1]) is int and 0 <= entry[1] <= 0o777, "invalid transaction mode"
        )
        require(
            (entry[0] == "directory" and len(entry) == 2)
            or (entry[0] == "file" and len(entry) == 3 and valid_sha(entry[2])),
            "invalid transaction file",
        )
        for parent in Path(name).parents:
            if parent != Path("."):
                require(
                    entries.get(str(parent), [None])[0] == "directory",
                    "invalid transaction hierarchy",
                )


def valid_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def transition_plan(before, after, kind, reverse=False):
    current = dict(before)
    result = []

    def append(name, action, entry):
        result.append(
            {
                "path": name,
                "action": action,
                "before": current.get(name),
                "after": entry,
            }
        )
        if entry is None:
            current.pop(name, None)
        else:
            current[name] = entry

    if kind == "bootstrap":
        for name in reversed(GUARDS) if reverse else GUARDS:
            append(name, "publish-file", after.get(name))
        return result
    for name in sorted(before, key=lambda item: (item.count("/"), item), reverse=True):
        if name not in after or before[name][0] != after[name][0]:
            append(
                name,
                "delete-directory" if before[name][0] == "directory" else "delete-file",
                None,
            )
    for name, entry in sorted(
        after.items(), key=lambda item: (item[0].count("/"), item[0])
    ):
        if entry[0] == "directory":
            if name not in current:
                append(name, "create-directory", ["directory", entry[1] | 0o700])
        elif current.get(name) != entry:
            append(name, "publish-file", entry)
    for name in sorted(after, key=lambda item: (item.count("/"), item), reverse=True):
        if after[name][0] == "directory" and current[name] != after[name]:
            append(name, "chmod-directory", after[name])
    require(current == after, "invalid transaction plan")
    return result


def apply_prefix(entries, plan, count):
    result = dict(entries)
    for operation in plan[:count]:
        name = operation["path"]
        require(
            result.get(name) == operation["before"], "invalid transaction transition"
        )
        if operation["after"] is None:
            result.pop(name, None)
        else:
            result[name] = operation["after"]
    return result


def apply_transition(source, production, operation):
    path = production / operation["path"]
    action = operation["action"]
    if action == "publish-file":
        publish_guard(source, production, operation["path"], operation["after"])
    elif action == "create-directory":
        temporary = Path(
            tempfile.mkdtemp(prefix=".promotion-state.directory.", dir=path.parent)
        )
        try:
            temporary.chmod(operation["after"][1])
            durable_directory(temporary)
            os.replace(temporary, path)
            durable_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.rmdir()
    else:
        if action == "delete-directory":
            path.rmdir()
        elif action == "delete-file":
            path.unlink()
        elif action == "chmod-directory":
            path.chmod(operation["after"][1])
            durable_directory(path)
        else:
            raise ValueError("unknown transaction action")
        durable_directory(path.parent)


def read_private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077,
            "unsafe transaction metadata",
        )
        result = json.load(stream)
    require(isinstance(result, dict), "invalid transaction metadata")
    return result


def write_state(transaction, state):
    sealed = {name: value for name, value in state.items() if name != "integrity"}
    sealed["integrity"] = digest(sealed)
    write_json(transaction / "state.json", sealed)


def begin_transaction(
    transaction,
    production,
    kind,
    baseline,
    candidate,
    old_entries,
    candidate_entries,
    baseline_compose=None,
    candidate_compose=None,
):
    info = (production / ".stack.lock").stat()
    manifest = {
        "schema": 1,
        "kind": kind,
        "production": str(production),
        "transaction": str(transaction),
        "lock_identity": [info.st_dev, info.st_ino],
        "baseline_entries": baseline,
        "candidate_entries": candidate,
        "old_snapshot_entries": old_entries,
        "candidate_snapshot_entries": candidate_entries,
        "baseline_compose_sha": baseline_compose,
        "candidate_compose_sha": candidate_compose,
    }
    write_json(transaction / "manifest.json", manifest)
    state = {
        "schema": 1,
        "manifest_sha": digest(manifest),
        "phase": "prepared",
        "direction": "forward",
        "origin_index": None,
        "index": 0,
        "pending": None,
    }
    write_state(transaction, state)
    write_json(
        production / ".promotion-state",
        {
            "schema": 1,
            "transaction": str(transaction),
            "manifest_sha": digest(manifest),
        },
    )
    return manifest, state


def progress_context(manifest, state):
    forward = transition_plan(
        manifest["baseline_entries"], manifest["candidate_entries"], manifest["kind"]
    )
    if state["direction"] == "forward":
        require(state["origin_index"] is None, "invalid forward origin")
        start, plan = manifest["baseline_entries"], forward
        allowed = {
            "prepared",
            "complete",
            "publishing" if manifest["kind"] == "bootstrap" else "promoting",
        }
        if manifest["kind"] == "promotion":
            allowed.add("deploying")
    else:
        require(
            state["direction"] == "reverse"
            and type(state["origin_index"]) is int
            and 0 <= state["origin_index"] <= len(forward),
            "invalid recovery origin",
        )
        start = apply_prefix(
            manifest["baseline_entries"], forward, state["origin_index"]
        )
        plan = transition_plan(
            start, manifest["baseline_entries"], manifest["kind"], reverse=True
        )
        allowed = {"recovering", "recovered"}
    require(
        state["phase"] in allowed
        and type(state["index"]) is int
        and 0 <= state["index"] <= len(plan),
        "invalid transaction progress",
    )
    if state["pending"] is not None:
        require(
            state["index"] < len(plan) and state["pending"] == plan[state["index"]],
            "invalid pending transition",
        )
    if state["phase"] in {"complete", "deploying", "recovered"}:
        require(
            state["index"] == len(plan) and state["pending"] is None,
            "unfinished terminal transaction",
        )
    if state["phase"] == "prepared":
        require(
            state["index"] == 0 and state["pending"] is None,
            "invalid prepared transaction",
        )
    return apply_prefix(start, plan, state["index"]), plan


def load_transaction(transaction, production):
    info = transaction.stat()
    require(
        info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077,
        "unsafe transaction directory",
    )
    manifest = read_private_json(transaction / "manifest.json")
    require(
        set(manifest)
        == {
            "schema",
            "kind",
            "production",
            "transaction",
            "lock_identity",
            "baseline_entries",
            "candidate_entries",
            "old_snapshot_entries",
            "candidate_snapshot_entries",
            "baseline_compose_sha",
            "candidate_compose_sha",
        }
        and type(manifest["schema"]) is int
        and manifest["schema"] == 1
        and isinstance(manifest["kind"], str)
        and manifest["kind"] in {"bootstrap", "promotion"}
        and manifest["production"] == str(production)
        and manifest["transaction"] == str(transaction),
        "invalid transaction schema or root",
    )
    lock = (production / ".stack.lock").stat()
    require(
        manifest["lock_identity"] == [lock.st_dev, lock.st_ino],
        "transaction lock identity changed",
    )
    for name in (
        "baseline_entries",
        "candidate_entries",
        "old_snapshot_entries",
        "candidate_snapshot_entries",
    ):
        validate_entries(manifest[name])
    if manifest["kind"] == "bootstrap":
        expected = dict(manifest["baseline_entries"])
        for name in GUARDS:
            require(
                manifest["candidate_entries"].get(name, [None])[0] == "file",
                "missing bootstrap guard",
            )
            require(
                name not in manifest["baseline_entries"]
                or manifest["baseline_entries"][name][0] == "file",
                "invalid old bootstrap guard",
            )
            expected[name] = manifest["candidate_entries"][name]
        require(
            expected == manifest["candidate_entries"]
            and guard_inventory(manifest["baseline_entries"])
            == manifest["old_snapshot_entries"]
            and guard_inventory(expected) == manifest["candidate_snapshot_entries"]
            and manifest["baseline_compose_sha"] is None
            and manifest["candidate_compose_sha"] is None,
            "invalid bootstrap manifest",
        )
    else:
        require(
            manifest["baseline_entries"] == manifest["old_snapshot_entries"]
            and manifest["candidate_entries"] == manifest["candidate_snapshot_entries"]
            and valid_sha(manifest["baseline_compose_sha"])
            and valid_sha(manifest["candidate_compose_sha"]),
            "invalid promotion manifest",
        )
        for name in GUARDS:
            require(
                name in manifest["baseline_entries"]
                and manifest["baseline_entries"][name]
                == manifest["candidate_entries"].get(name),
                "transaction guards changed",
            )
    for directory, key in (
        ("old", "old_snapshot_entries"),
        ("candidate", "candidate_snapshot_entries"),
    ):
        require(
            inventory(canonical(str(transaction / directory))) == manifest[key],
            "transaction snapshot changed",
        )
    state = read_private_json(transaction / "state.json")
    require(
        set(state)
        == {
            "schema",
            "manifest_sha",
            "phase",
            "direction",
            "origin_index",
            "index",
            "pending",
            "integrity",
        }
        and type(state["schema"]) is int
        and state["schema"] == 1
        and isinstance(state["phase"], str)
        and isinstance(state["direction"], str)
        and state["manifest_sha"] == digest(manifest)
        and state["integrity"]
        == digest({key: value for key, value in state.items() if key != "integrity"}),
        "invalid transaction journal integrity",
    )
    progress_context(manifest, state)
    if os.path.lexists(production / ".promotion-state"):
        require(
            read_private_json(production / ".promotion-state")
            == {
                "schema": 1,
                "transaction": str(transaction),
                "manifest_sha": digest(manifest),
            },
            "wrong transaction marker",
        )
    return manifest, state


def attributed_progress(production, manifest, state):
    expected, plan = progress_context(manifest, state)
    current = inventory(production)
    if state["pending"] is not None:
        following = dict(expected)
        operation = state["pending"]
        if operation["after"] is None:
            following.pop(operation["path"], None)
        else:
            following[operation["path"]] = operation["after"]
        if current == following:
            return state["index"] + 1, following, plan
    require(current == expected, "live tree is outside recorded transaction progress")
    return state["index"], expected, plan


def run_transitions(transaction, production, manifest, state, source):
    index, expected, plan = attributed_progress(production, manifest, state)
    if state["pending"] is not None:
        state.update(index=index, pending=None)
        write_state(transaction, state)
    for operation in plan[index:]:
        require(
            inventory(production) == expected, "live tree changed before transition"
        )
        state["pending"] = operation
        write_state(transaction, state)
        apply_transition(source, production, operation)
        expected = apply_prefix(expected, [operation], 1)
        require(
            inventory(production) == expected, "live tree changed during transition"
        )
        state.update(index=state["index"] + 1, pending=None)
        write_state(transaction, state)


def clear_marker(transaction, production, manifest):
    pointer = production / ".promotion-state"
    if os.path.lexists(pointer):
        require(
            read_private_json(pointer)
            == {
                "schema": 1,
                "transaction": str(transaction),
                "manifest_sha": digest(manifest),
            },
            "wrong transaction marker",
        )
        pointer.unlink()
    durable_directory(production)


def finish_transaction(transaction, production, manifest, state, phase):
    state["phase"] = phase
    expected, _ = progress_context(manifest, state)
    require(inventory(production) == expected, "live tree changed before completion")
    write_state(transaction, state)
    clear_marker(transaction, production, manifest)


def reconcile(args, production):
    transaction = transaction_path(args.transaction, production)
    manifest, state = load_transaction(transaction, production)
    require(
        state["phase"] in {"complete", "recovered"},
        "transaction is not terminal; explicit recovery required",
    )
    expected, _ = progress_context(manifest, state)
    require(
        inventory(production) == expected,
        "live tree changed after transaction completion",
    )
    if state["phase"] == "complete" and manifest["kind"] == "promotion":
        require(
            compose_sha(production) == manifest["candidate_compose_sha"],
            "completed render changed",
        )
    clear_marker(transaction, production, manifest)


def bootstrap(args, production):
    require(
        args.maintenance_window, "bootstrap requires cooperative maintenance exclusion"
    )
    stage = canonical(args.stage)
    require(
        not stage.is_relative_to(production) and not production.is_relative_to(stage),
        "stage and production must be separate",
    )
    transaction = transaction_path(args.transaction, production, stage)
    require(
        not os.path.lexists(production / ".promotion-state"),
        "pending operation requires recovery",
    )
    require(not transaction.exists(), "transaction already exists")
    baseline = inventory(production)
    candidate = inventory(stage, stage=True)
    require(digest(baseline) == args.baseline_sha, "production baseline mismatch")
    require(digest(candidate) == args.stage_sha, "stage identity mismatch")
    require(
        baseline.get("ops", [None])[0] == "directory",
        "missing production ops directory",
    )
    for name in GUARDS:
        require(candidate.get(name, [None])[0] == "file", "missing candidate guard")
        require(name not in baseline or baseline[name][0] == "file", "unsafe old guard")
    candidate_guards = guard_inventory(candidate)
    candidate_guards["ops"] = baseline["ops"]
    transaction.mkdir(mode=0o700)
    durable_directory(transaction.parent)
    for directory, source, entries in (
        ("old", production, guard_inventory(baseline)),
        ("candidate", stage, candidate_guards),
    ):
        target = transaction / directory
        target.mkdir(mode=0o700)
        synchronize(source, target, entries)
    require(
        inventory(production) == baseline and inventory(stage, stage=True) == candidate,
        "bootstrap inputs changed while copying",
    )
    expected = dict(baseline)
    expected.update({name: candidate_guards[name] for name in GUARDS})
    manifest, state = begin_transaction(
        transaction,
        production,
        "bootstrap",
        baseline,
        expected,
        guard_inventory(baseline),
        candidate_guards,
    )
    state["phase"] = "publishing"
    write_state(transaction, state)
    run_transitions(transaction, production, manifest, state, transaction / "candidate")
    finish_transaction(transaction, production, manifest, state, "complete")


def render(args, production):
    stage = canonical(args.stage)
    require(
        not stage.is_relative_to(production) and not production.is_relative_to(stage),
        "stage and production must be separate",
    )
    private = transaction_path(args.private_render, production, stage)
    require(not private.exists(), "private render directory already exists")
    require(
        not os.path.lexists(production / ".promotion-state"),
        "pending operation requires recovery",
    )
    candidate = inventory(stage, stage=True)
    baseline = inventory(production)
    require(digest(candidate) == args.stage_sha, "stage identity mismatch")
    require(digest(baseline) == args.baseline_sha, "production baseline mismatch")
    descriptor = os.open(production / ".env", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        require(
            stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
            and os.fstat(stream.fileno()).st_nlink == 1,
            "unsafe private environment file",
        )
        private_bytes = stream.read()
    private.mkdir(mode=0o700)
    synchronize(stage, private, candidate)
    descriptor = os.open(private / ".env", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(private_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    durable_directory(private)
    result = compose_sha(private, production)
    require(
        inventory(stage, stage=True) == candidate and inventory(production) == baseline,
        "render inputs changed",
    )
    require(
        (production / ".env").read_bytes() == private_bytes,
        "private environment changed",
    )
    print(result)


def admit(production):
    pointer = production / ".promotion-state"
    if not os.path.lexists(pointer):
        return
    require(not pointer.is_symlink(), "unsafe promotion state")
    marker = read_private_json(pointer)
    require(isinstance(marker.get("transaction"), str), "invalid transaction marker")
    transaction = transaction_path(marker["transaction"], production)
    manifest, state = load_transaction(transaction, production)
    require(
        manifest["kind"] == "promotion" and state["phase"] == "deploying",
        "pending promotion requires recovery",
    )
    attributed_progress(production, manifest, state)
    require(
        os.environ.get("MEDIA_GATEWAY_PROMOTION") == str(transaction),
        "deployment requires promotion admission",
    )
    descriptor = int(os.environ.get("MEDIA_GATEWAY_PROMOTION_FD", "-1"))
    info = os.fstat(descriptor)
    lock = production / ".stack.lock"
    require(
        (info.st_dev, info.st_ino) == (lock.stat().st_dev, lock.stat().st_ino),
        "foreign promotion lock",
    )
    probe = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise ValueError("promotion lock is not held")
    finally:
        os.close(probe)


def promote(args, production, descriptor):
    stage = canonical(args.stage)
    require(
        not stage.is_relative_to(production) and not production.is_relative_to(stage),
        "stage and production must be separate",
    )
    transaction = transaction_path(args.transaction, production, stage)
    require(
        not os.path.lexists(production / ".promotion-state"),
        "pending promotion requires recovery",
    )
    require(not transaction.exists(), "transaction already exists")
    candidate = inventory(stage, stage=True)
    baseline = inventory(production)
    for name in GUARDS:
        require(
            name in baseline and candidate.get(name) == baseline[name],
            "install and retain the reviewed promotion guards before staging",
        )
    require(digest(candidate) == args.stage_sha, "stage identity mismatch")
    require(digest(baseline) == args.baseline_sha, "production baseline mismatch")
    require(
        compose_sha(production) == args.baseline_compose_sha,
        "rendered baseline mismatch",
    )
    transaction.mkdir(mode=0o700)
    durable_directory(transaction.parent)
    old = transaction / "old"
    prepared = transaction / "candidate"
    old.mkdir(mode=0o700)
    prepared.mkdir(mode=0o700)
    synchronize(production, old, baseline)
    synchronize(stage, prepared, candidate)
    require(inventory(stage, stage=True) == candidate, "stage changed while copying")
    require(inventory(production) == baseline, "production changed while copying")
    manifest, state = begin_transaction(
        transaction,
        production,
        "promotion",
        baseline,
        candidate,
        baseline,
        candidate,
        args.baseline_compose_sha,
        args.candidate_compose_sha,
    )
    state["phase"] = "promoting"
    write_state(transaction, state)
    run_transitions(transaction, production, manifest, state, prepared)
    require(
        compose_sha(production) == args.candidate_compose_sha,
        "candidate rendered hash mismatch",
    )
    state["phase"] = "deploying"
    write_state(transaction, state)
    environment = dict(
        os.environ,
        MEDIA_GATEWAY_LOCK_HELD="1",
        MEDIA_GATEWAY_LOCKFILE=str(production / ".stack.lock"),
        MEDIA_GATEWAY_PROMOTION=str(transaction),
        MEDIA_GATEWAY_PROMOTION_FD=str(descriptor),
        EXPECTED_COMPOSE_SHA256=args.candidate_compose_sha,
    )
    command = ["bash", str(production / "ops/deploy.sh")]
    if args.no_tunnel:
        command.append("--no-tunnel")
    subprocess.run(
        command, cwd=production, env=environment, pass_fds=(descriptor,), check=True
    )
    finish_transaction(transaction, production, manifest, state, "complete")


def recover(args, production):
    transaction = transaction_path(args.transaction, production)
    manifest, state = load_transaction(transaction, production)
    if state["phase"] == "recovered":
        reconcile(args, production)
        return
    require(
        state["phase"] != "complete",
        "completed transaction requires reconcile, never file rollback",
    )
    require(
        state["phase"] != "deploying",
        "deployment may have started: use stopped-writer data and offline recovery",
    )
    require(
        os.path.lexists(production / ".promotion-state"), "missing transaction marker"
    )
    index, _, _ = attributed_progress(production, manifest, state)
    if state["direction"] == "forward":
        state.update(
            direction="reverse",
            origin_index=index,
            index=0,
            pending=None,
            phase="recovering",
        )
        write_state(transaction, state)
    run_transitions(transaction, production, manifest, state, transaction / "old")
    finish_transaction(transaction, production, manifest, state, "recovered")


def parser():
    result = argparse.ArgumentParser(
        description="Promote reviewed non-secret files under the project lock"
    )
    commands = result.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("identity")
    identity.add_argument("--root", required=True)
    promotion = commands.add_parser("promote")
    promotion.add_argument("--stage", required=True)
    for name in (
        "stage-sha",
        "baseline-sha",
        "baseline-compose-sha",
        "candidate-compose-sha",
    ):
        promotion.add_argument("--" + name, required=True)
    promotion.add_argument("--no-tunnel", action="store_true")
    installation = commands.add_parser("bootstrap")
    installation.add_argument(
        "--maintenance-window", action="store_true", required=True
    )
    rendering = commands.add_parser("render")
    rendering.add_argument("--private-render", required=True)
    for command in (installation, rendering):
        command.add_argument("--stage", required=True)
        command.add_argument("--stage-sha", required=True)
        command.add_argument("--baseline-sha", required=True)
    recovery = commands.add_parser("recover")
    reconciliation = commands.add_parser("reconcile")
    admission = commands.add_parser("admit")
    for command in (
        promotion,
        recovery,
        admission,
        installation,
        rendering,
        reconciliation,
    ):
        command.add_argument("--production", required=True)
    for command in (promotion, recovery, installation, reconciliation):
        command.add_argument("--transaction", required=True)
    return result


def execute(args):
    if args.command == "identity":
        print(digest(inventory(canonical(args.root))))
        return
    production = canonical(args.production)
    require(production.is_dir(), "missing production directory")
    if args.command == "admit":
        admit(production)
        return
    with locked(production) as descriptor:
        if args.command == "promote":
            promote(args, production, descriptor)
        elif args.command == "bootstrap":
            bootstrap(args, production)
        elif args.command == "render":
            render(args, production)
        elif args.command == "reconcile":
            reconcile(args, production)
        else:
            recover(args, production)


if __name__ == "__main__":
    try:
        execute(parser().parse_args())
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(
            f"promotion refused: {detail}; inspect local transaction state",
            file=sys.stderr,
        )
        sys.exit(1)
