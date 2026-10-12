import base64
import http.client
import http.cookiejar
import json
import os
import random
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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


class RequestError(Exception):
    def __init__(self, method, status=None, committed_unknown=False):
        self.method = method
        self.status = status
        self.committed_unknown = committed_unknown
        super().__init__(
            f"{method} request failed: "
            + (f"HTTP {status}" if status is not None else "no response")
        )


def retry_after(value, now=None):
    now = datetime.now(UTC) if now is None else now
    try:
        if value is None:
            return 60
        if re.fullmatch(r"[0-9]+", value.strip()):
            seconds = int(value)
        else:
            date = parsedate_to_datetime(value)
            seconds = (date - now).total_seconds()
            if seconds <= 0:
                return 60
        return max(1, min(330, seconds))
    except (TypeError, ValueError, OverflowError):
        return 60


SAFE_RETRY_METHODS = ("GET", "HEAD")


class Http:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(
        self,
        method,
        path,
        body=None,
        headers=None,
        raw=None,
        timeout=120,
        retry_429=True,
    ):
        data = (
            raw
            if raw is not None
            else (json.dumps(body).encode() if body is not None else None)
        )
        hdrs = {"User-Agent": UA, "Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=hdrs
        )
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=timeout) as response:
                    payload = response.read()
                return (
                    json.loads(payload)
                    if payload and payload[:1] in (b"{", b"[")
                    else payload
                )
            except urllib.error.HTTPError as error:
                if error.code == 429 and retry_429 and attempt < 3:
                    delay = retry_after(
                        error.headers.get("Retry-After")
                    ) + random.uniform(0, 5)
                    error.close()
                    print(f"{method} request rate limited; retry {attempt + 1}/3")
                    time.sleep(delay)
                    continue
                if (
                    error.code in (502, 503, 504)
                    and method in SAFE_RETRY_METHODS
                    and retry_429
                    and attempt < 3
                ):
                    error.close()
                    time.sleep(5)
                    continue
                status = error.code
                error.close()
                raise RequestError(method, status, method == "PUT") from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise RequestError(method, None, method == "PUT") from None


def reconciliation_get(http, headers):
    for attempt, delay in enumerate((5, 15, 30, None)):
        try:
            return http.call("GET", "/api/v1/user", headers=headers, retry_429=False)
        except RequestError as error:
            if delay is None or (
                error.status is not None and error.status != 429 and error.status < 500
            ):
                raise
            print(f"GET reconciliation retry {attempt + 1}/3")
            time.sleep(delay)


CONFIG_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
SERVER_MAINTAINED_KEYS = (
    "statistics",
    "trusted",
    "ip",
    "activeVariants",
    "autoVariants",
    "healthResults",
    "variantSelectorLocation",
)


def load_configs(root, main_uuid=None):
    root = Path(root)
    path = root / "config" / "configs.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return []
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise SystemExit(
            "config/configs.json must be a private regular file owned by the current user"
        )
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit("config/configs.json cannot be read as JSON") from None
    if (
        not isinstance(body, dict)
        or set(body) != {"configs"}
        or not isinstance(body["configs"], list)
    ):
        raise SystemExit("config/configs.json must contain only a configs list")
    if main_uuid is None:
        main_uuid = Settings(root).get("AIOSTREAMS_UUID")
    names, uuids = set(), {main_uuid}
    for entry in body["configs"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "uuid", "password"}:
            raise SystemExit("each config must contain only name, uuid, and password")
        if any(
            not isinstance(value, str) or not value.strip() for value in entry.values()
        ):
            raise SystemExit("config fields must be non-empty strings")
        name, uuid = entry["name"], entry["uuid"]
        if (
            not CONFIG_NAME.fullmatch(name)
            or name == "main"
            or name in names
            or uuid in uuids
        ):
            raise SystemExit(
                "config names and UUIDs must be valid and unique; main is reserved"
            )
        names.add(name)
        uuids.add(uuid)
    return body["configs"]


def resolve_target(settings, name=None):
    if name is None or name == "main":
        return (
            "main",
            settings.get("AIOSTREAMS_UUID"),
            settings.get("AIOSTREAMS_PASSWORD"),
            True,
        )
    configs = load_configs(settings.root, settings.get("AIOSTREAMS_UUID"))
    for entry in configs:
        if entry["name"] == name:
            return name, entry["uuid"], entry["password"], False
    raise SystemExit(
        "unknown config; known names: "
        + ", ".join(["main", *(entry["name"] for entry in configs)])
    )


def canonical_config(config):
    return json.dumps(
        {
            key: value
            for key, value in config.items()
            if key not in SERVER_MAINTAINED_KEYS
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def private_directory(path):
    path = Path(path).expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        missing = []
        parent = path
        while not parent.exists() and not parent.is_symlink():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise SystemExit(
            "snapshot directory must be private, owned by the current user, and not a symlink"
        )
    return path


def save_snapshot(path, config, target):
    path = Path(path).expanduser()
    private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f"snapshot already exists: {path}") from None
    with os.fdopen(descriptor, "w") as output:
        json.dump(
            {"target": {"name": target[0], "uuid": target[1]}, "config": config}, output
        )
        output.write("\n")
    return path


def snapshot_path(directory, name):
    directory = private_directory(directory)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{name}-{timestamp}-{secrets.token_hex(4)}.json"


def aiostreams_session(aio, auth):
    user, separator, password = auth.partition(":")
    if not separator or not user or not password:
        raise SystemExit("AIOSTREAMS_AUTH must use username:password format")
    aio.call("POST", "/api/v1/auth/login", {"username": user, "password": password})


def basic(uuid, pw):
    return {
        "Authorization": "Basic " + base64.b64encode(f"{uuid}:{pw}".encode()).decode()
    }


class QuietHttp(Http):
    pass
