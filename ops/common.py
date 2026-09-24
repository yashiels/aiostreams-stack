import base64
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "curl/8.7.1"


class Plan:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.changes = 0

    def change(self, what):
        self.changes += 1
        print(("would " if self.dry_run else "") + what)
        return not self.dry_run

    def ok(self, what):
        print(f"ok   {what}")


class Settings:
    def __init__(self, root):
        self.root = Path(root)
        self.file_values = self._load_file()

    def _load_file(self):
        env_file = self.root / ".env"
        if not env_file.is_file():
            return {}
        exporter = self.root / "ops" / "env-export.py"
        output = subprocess.run(
            [sys.executable, str(exporter), str(env_file)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        values = {}
        for line in output.splitlines():
            if not line.startswith("export "):
                continue
            key, _, encoded = line[7:].partition("=")
            values[key] = decode_shell_value(encoded)
        return values

    def get(self, key, required=False):
        value = os.environ[key] if key in os.environ else self.file_values.get(key, "")
        if required and not value.strip():
            raise SystemExit(f"{key} is missing or empty")
        return value


def decode_shell_value(value):
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("'\"'\"'", "'")
    return value


class Http:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, headers=None, raw=None, timeout=120):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"User-Agent": UA, "Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    payload = r.read()
                return json.loads(payload) if payload and payload[:1] in (b"{", b"[") else payload
            except urllib.error.HTTPError as e:
                if e.code in (502, 503, 504) and attempt < 3:
                    time.sleep(5)
                    continue
                detail = e.read()[:300].decode(errors="replace")
                raise SystemExit(f"{method} {path}: HTTP {e.code} {detail}") from None


def aiostreams_session(aio, auth):
    user, separator, password = auth.partition(":")
    if not separator or not user or not password:
        raise SystemExit("AIOSTREAMS_AUTH must use username:password format")
    aio.call("POST", "/api/v1/auth/login", {"username": user, "password": password})


def basic(uuid, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{uuid}:{pw}".encode()).decode()}
