#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

python3 - <<'PY'
import base64
import re
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
excluded = {"ops/leak-gate.sh", "ops/leak-patterns.txt"}
patterns = [base64.b64decode(line).lower() for line in (root / "ops/leak-patterns.txt").read_bytes().splitlines() if line]
hex_run = re.compile(r"[0-9a-fA-F]{32,}")
image_line = re.compile(r"[A-Z0-9_]+_IMAGE=.*@sha256:[0-9a-f]{64}")
digest = re.compile(r"@sha256:[0-9a-f]{64}")
tmdb_ref = re.compile(r"(TMDB_ADDON_REF=)[0-9a-fA-F]{40}\b")
tmdb_dockerfile = "images/tmdb-addon/Dockerfile"
failures = []


def check_patterns(label, data):
    lowered = data.lower()
    for pattern in patterns:
        if pattern in lowered:
            failures.append(f"{label}: blocked pattern {base64.b64encode(pattern).decode()}")


def check_hex(label, line, allow_digest=False, allow_tmdb_ref=False, allow_tmdb_digest=False):
    candidate = digest.sub("", line) if allow_digest and image_line.fullmatch(line) else line
    if allow_tmdb_ref:
        candidate = tmdb_ref.sub(r"\1", candidate)
    if allow_tmdb_digest:
        candidate = digest.sub("", candidate)
    if hex_run.search(candidate):
        failures.append(f"{label}: disallowed hexadecimal run")


for path in root.rglob("*"):
    if not path.is_file():
        continue
    relative = path.relative_to(root).as_posix()
    parts = path.relative_to(root).parts
    if relative in excluded or any(part in {".git", ".ruff_cache", "__pycache__"} for part in parts):
        continue
    data = path.read_bytes()
    check_patterns(relative, data)
    for number, line in enumerate(data.decode(errors="replace").splitlines(), start=1):
        check_hex(
            f"{relative}:{number}",
            line,
            relative == "versions.env",
            relative in {"versions.env", tmdb_dockerfile},
            relative == tmdb_dockerfile,
        )


def git_output(*args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return result.stdout


history = git_output(
    "log",
    "--all",
    "--format=",
    "-p",
    "--",
    ".",
    ":(exclude)ops/leak-gate.sh",
    ":(exclude)ops/leak-patterns.txt",
)
check_patterns("git history", history)
history_path = ""
in_hunk = False
for number, line in enumerate(history.decode(errors="replace").splitlines(), start=1):
    if line.startswith("diff --git a/"):
        match = re.match(r"diff --git a/(.+) b/(.+)", line)
        history_path = match.group(2) if match else ""
        in_hunk = False
        continue
    if line.startswith("@@"):
        in_hunk = True
        continue
    if not in_hunk or not line.startswith((" ", "+", "-")) or line.startswith(("+++", "---")):
        continue
    content = line[1:]
    check_hex(
        f"git history:{number}",
        content,
        history_path == "versions.env",
        history_path in {"versions.env", tmdb_dockerfile},
        history_path == tmdb_dockerfile,
    )

metadata = git_output("log", "--all", "--format=%s %b")
authors = git_output("log", "--all", "--format=%an <%ae> %cn <%ce>")
tags = git_output("tag", "-l")
check_patterns("git metadata", metadata)
check_patterns("git tags", tags)
for object_id in git_output("rev-list", "--all").splitlines():
    headers, message = git_output("--no-replace-objects", "cat-file", "commit", object_id.decode()).split(b"\n\n", 1)
    parents = [line.removeprefix(b"parent ") for line in headers.splitlines() if line.startswith(b"parent ")]
    check_patterns("git metadata", message)
    if len(parents) == 2:
        canonical = b"Merge " + parents[1] + b" into " + parents[0]
        if message in (canonical, canonical + b"\n"):
            message = message.replace(parents[1], b"", 1).replace(parents[0], b"", 1)
    for number, line in enumerate(message.decode(errors="replace").splitlines(), start=1):
        check_hex(f"git metadata:{number}", line)

for label, data in (("git authors", authors), ("git tags", tags)):
    for number, line in enumerate(data.decode(errors="replace").splitlines(), start=1):
        check_hex(f"{label}:{number}", line)

if failures:
    print("\n".join(dict.fromkeys(failures)), file=sys.stderr)
    raise SystemExit(1)
PY
