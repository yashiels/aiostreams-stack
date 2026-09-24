#!/usr/bin/env python3
import shlex
import sys
from pathlib import Path


def decode(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        body = value[1:-1]
        out = []
        i = 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out.append(body[i + 1])
                i += 2
                continue
            if body[i : i + 2] == "$$":
                out.append("$")
                i += 2
                continue
            out.append(body[i])
            i += 1
        return "".join(out)
    return value


if __name__ == "__main__":
    for path in sys.argv[1:]:
        f = Path(path)
        if not f.is_file():
            continue
        for raw in f.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.replace("_", "").isalnum():
                continue
            print(f"export {key}={shlex.quote(decode(value))}")
