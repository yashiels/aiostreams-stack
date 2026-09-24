#!/usr/bin/env python3
import argparse
import json
import re
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import Http, Settings, basic

ROOT = Path(__file__).resolve().parent.parent
CLIENT = 'MediaBrowser Client="media-gateway-verify", Device="verify", DeviceId="media-gateway-verify", Version="1.0"'
MIB = 1024 * 1024
SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._/~-]+$")


class CheckError(Exception):
    pass


class Reporter:
    def __init__(self):
        self.failures = 0

    def check(self, number, label, action):
        try:
            result = action()
        except Exception as error:  # noqa: BLE001
            self.failures += 1
            print(f"FAIL {number} {label}: {error}")
            return None
        print(f"PASS {number} {label}")
        return result

    def info(self, label, value):
        print(f"INFO {label}: {value}")


def jellyfin_base(url):
    return url.rstrip("/") if url.rstrip("/").endswith("/jellyfin") else f"{url.rstrip('/')}/jellyfin"


def request_json(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {"User-Agent": "media-gateway-verify/1.0", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise CheckError(f"HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        detail = getattr(error, "reason", type(error).__name__)
        raise CheckError(f"network error: {detail}") from None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise CheckError("response was not JSON") from None


def token_headers(token):
    return {"Authorization": f'{CLIENT}, Token="{token}"'}


def jellyfin_info(root_url):
    info = request_json("GET", f"{jellyfin_base(root_url)}/System/Info/Public")
    if info.get("ProductName") != "Jellyfin Server":
        raise CheckError("ProductName is not Jellyfin Server")
    return info


def retired_url_absent(retired_url):
    for suffix in ("/System/Info/Public", "/jellyfin/System/Info/Public"):
        request = urllib.request.Request(
            f"{retired_url.rstrip('/')}{suffix}",
            headers={"User-Agent": "media-gateway-verify/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        try:
            info = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(info, dict) and info.get("ProductName") == "Jellyfin Server":
            raise CheckError(f"{retired_url}{suffix} still serves Jellyfin")


def authenticate(base, uuid, password, name):
    response = request_json(
        "POST",
        f"{base}/Users/AuthenticateByName",
        {"Username": f"{uuid}/{name}", "Pw": password},
        {"Authorization": CLIENT},
    )
    token = response.get("AccessToken")
    user = response.get("User") or {}
    if not token or not user.get("Id"):
        raise CheckError(f"{name} did not receive a token")
    return {"token": token, "user_id": user["Id"], "name": name}


def authenticate_household(base, uuid, password, names):
    sessions = {}
    for name in names:
        try:
            sessions[name] = authenticate(base, uuid, password, name)
        except CheckError as error:
            raise CheckError(f"{name}: {error}") from None
    return sessions


def picker_names(picker_url, expected):
    users = request_json("GET", f"{picker_url.rstrip('/')}/Users/Public")
    names = [user.get("Name") for user in users]
    if sorted(names) != sorted(expected):
        raise CheckError(f"picker returned {len(names)} users, expected {len(expected)}")


def session_json(base, session, method, path, body=None, timeout=30):
    return request_json(method, f"{base}{path}", body, token_headers(session["token"]), timeout)


def items_from(response):
    return response.get("Items", []) if isinstance(response, dict) else []


def find_title(base, session, query, item_type, imdb_id, exact_name):
    params = urllib.parse.urlencode(
        {
            "SearchTerm": query,
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "Limit": "24",
        }
    )
    items = items_from(session_json(base, session, "GET", f"/Items?{params}"))
    for item in items:
        if imdb_id in (item.get("ProviderIds") or {}).values():
            return item
    for item in items:
        if str(item.get("Name", "")).casefold() == exact_name.casefold():
            return item
    raise CheckError(f"{imdb_id} not found")


def first_episode(base, session, series):
    params = urllib.parse.urlencode({"Season": 1, "Limit": 1000})
    episodes = items_from(session_json(base, session, "GET", f"/Shows/{series['Id']}/Episodes?{params}"))
    for episode in episodes:
        if episode.get("ParentIndexNumber") == 1 and episode.get("IndexNumber") == 1:
            return episode
    raise CheckError("S1E1 not found")


def range_responds(path):
    request = urllib.request.Request(
        path,
        headers={"User-Agent": "media-gateway-verify/1.0", "Range": "bytes=0-1048575"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            response.read(1)
    except urllib.error.HTTPError as error:
        raise CheckError(f"source returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise CheckError(f"source request failed: {type(error).__name__}") from None
    elapsed = time.monotonic() - started
    if status != 206:
        raise CheckError(f"source returned HTTP {status}, expected 206")
    if elapsed > 10:
        raise CheckError(f"source took {elapsed:.1f}s, expected <= 10s")


def playable_source(base, session, item):
    response = session_json(base, session, "POST", f"/Items/{item['Id']}/PlaybackInfo", {}, timeout=120)
    sources = [
        source
        for source in response.get("MediaSources", [])
        if source.get("Protocol") == "Http"
        and source.get("IsRemote") is True
        and str(source.get("Path", "")).startswith(("http://", "https://"))
    ]
    if not sources:
        raise CheckError("PlaybackInfo returned no remote HTTP source")
    failures = []
    for source in sources[:3]:
        try:
            range_responds(source["Path"])
            return source["Path"]
        except CheckError as error:
            failures.append(str(error))
    raise CheckError(f"first {min(3, len(sources))} sources failed: {'; '.join(failures)}")


def verify_playback(base, session):
    movie = find_title(base, session, "Interstellar", "Movie", "tt0816692", "Interstellar")
    first_series = find_title(base, session, "Breaking Bad", "Series", "tt0903747", "Breaking Bad")
    second_series = find_title(base, session, "Severance", "Series", "tt11280740", "Severance")
    playable_source(base, session, first_episode(base, session, first_series))
    playable_source(base, session, first_episode(base, session, second_series))
    return {
        "movie": movie,
        "movie_source": playable_source(base, session, movie),
        "first_series": first_series,
    }


def ssh_tx_bytes(host, project):
    container = f"{project}-aiostreams-1"
    result = subprocess.run(
        [
            "ssh",
            host,
            shlex.join(
                [
                    "docker",
                    "exec",
                    container,
                    "/nodejs/bin/node",
                    "-e",
                    "process.stdout.write(require('fs').readFileSync('/sys/class/net/eth0/statistics/tx_bytes','utf8'))",
                ]
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise CheckError("remote TX counter was not an integer") from None


def download_64_mib(path):
    request = urllib.request.Request(
        path,
        headers={"User-Agent": "media-gateway-verify/1.0", "Range": f"bytes=0-{64 * MIB - 1}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 206:
                raise CheckError(f"range download returned HTTP {response.status}")
            total = 0
            while total < 64 * MIB:
                chunk = response.read(min(MIB, 64 * MIB - total))
                if not chunk:
                    break
                total += len(chunk)
    except urllib.error.HTTPError as error:
        raise CheckError(f"range download returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise CheckError(f"range download failed: {type(error).__name__}") from None
    if total != 64 * MIB:
        raise CheckError(f"range download returned {total} bytes, expected {64 * MIB}")


def no_relay(host, project, path):
    before = ssh_tx_bytes(host, project)
    download_64_mib(path)
    delta = ssh_tx_bytes(host, project) - before
    if delta < 0:
        raise CheckError("TX counter decreased")
    if delta >= 8 * MIB:
        raise CheckError(f"container transmitted {delta / MIB:.1f} MiB")
    return delta


def search_three_times(base, session):
    params = urllib.parse.urlencode(
        {"searchTerm": "succession", "IncludeItemTypes": "Movie,Series", "Recursive": "true", "Limit": "24"}
    )
    latencies = []
    for _ in range(3):
        started = time.monotonic()
        items = items_from(session_json(base, session, "GET", f"/Items?{params}", timeout=30))
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        if elapsed > 30:
            raise CheckError(f"search took {elapsed:.1f}s, expected <= 30s")
        if not items:
            raise CheckError("search returned no items")
    return latencies


def next_up_s1e1(base, session, series):
    params = urllib.parse.urlencode({"SeriesId": series["Id"], "Limit": 12})
    items = items_from(session_json(base, session, "GET", f"/Shows/NextUp?{params}"))
    if not items:
        raise CheckError("Next Up returned no items")
    first = items[0]
    if first.get("ParentIndexNumber") != 1 or first.get("IndexNumber") != 1:
        raise CheckError("Next Up did not return S1E1")


def wait_for_health(url, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            jellyfin_info(url)
            return
        except CheckError:
            time.sleep(10)
    raise CheckError("AIOStreams did not become healthy after restart")


def durable_write(base, public_url, host, remote_dir, project, session, movie):
    if not SAFE_REMOTE.fullmatch(remote_dir):
        raise CheckError("remote directory contains unsafe characters")
    user_id = session["user_id"]
    item_id = movie["Id"]
    marked = False
    failure = None
    try:
        session_json(base, session, "POST", f"/Users/{user_id}/PlayedItems/{item_id}", {})
        marked = True
        command = f"cd {remote_dir} && docker compose -p {project} --env-file .env --env-file versions.env -f compose.yml restart aiostreams"
        subprocess.run(["ssh", host, command], capture_output=True, text=True, check=True)
        wait_for_health(public_url)
        item = session_json(base, session, "GET", f"/Users/{user_id}/Items/{item_id}")
        if not (item.get("UserData") or {}).get("Played"):
            raise CheckError("played state did not survive restart")
    except (CheckError, subprocess.CalledProcessError) as error:
        failure = error
    finally:
        if marked:
            try:
                session_json(base, session, "DELETE", f"/Users/{user_id}/PlayedItems/{item_id}")
            except CheckError as cleanup_error:
                if failure is None:
                    failure = CheckError(f"could not unmark durable-write item: {cleanup_error}")
    if failure is not None:
        if isinstance(failure, subprocess.CalledProcessError):
            raise CheckError("remote AIOStreams restart failed") from None
        raise failure


def unavailable(message):
    raise CheckError(message)


def main():
    parser = argparse.ArgumentParser(description="Verify an AIOStreams native Jellyfin deployment.")
    parser.add_argument("--url", help="AIOStreams root URL; defaults to AIOSTREAMS_PUBLIC_URL")
    parser.add_argument("--retired-url", action="append", default=[], help="retired root URL, repeatable")
    parser.add_argument("--ssh-host", default="user@host", help="SSH host for container checks")
    parser.add_argument("--remote-dir", default="~/apps/media-gateway", help="remote stack directory")
    parser.add_argument("--project", default="media-gateway", help="Compose project name")
    args = parser.parse_args()

    settings = Settings(ROOT)
    url = (args.url or settings.get("AIOSTREAMS_PUBLIC_URL", required=True)).rstrip("/")
    uuid = settings.get("AIOSTREAMS_UUID", required=True)
    password = settings.get("AIOSTREAMS_PASSWORD", required=True)
    household_path = ROOT / "config" / "household.json"
    if not household_path.is_file():
        print("FAIL setup config/household.json is missing")
        return 1
    household = json.loads(household_path.read_text())
    try:
        response = Http(url).call("GET", "/api/v1/user", headers=basic(uuid, password))
        encrypted_password = response["data"]["encryptedPassword"]
    except (KeyError, TypeError, SystemExit) as error:
        print(f"FAIL setup could not derive picker URL: {error}")
        return 1
    picker_url = f"{url}/jellyfin/{uuid}/{encrypted_password}"
    primary = next((user for user in household["users"] if user.get("admin")), None)
    personas = [user for user in household["users"] if not user.get("admin")]
    if primary is None or not personas:
        print("FAIL setup household needs one admin and at least one persona")
        return 1
    names = [primary["name"], *[user["name"] for user in personas]]
    test_persona = personas[-1]["name"]
    base = jellyfin_base(url)
    reporter = Reporter()

    reporter.check(1, "Jellyfin endpoint", lambda: jellyfin_info(url))
    for index, retired in enumerate(dict.fromkeys(args.retired_url), start=1):
        reporter.check(f"2.{index}", f"retired {retired}", lambda retired=retired: retired_url_absent(retired))
    sessions = reporter.check(4, "primary and persona sign-ins", lambda: authenticate_household(base, uuid, password, names))
    reporter.check(5, f"picker lists exactly {len(names)} household users", lambda: picker_names(picker_url, names))
    playback = reporter.check(6, "fixed titles return direct playable sources", lambda: verify_playback(base, sessions[primary["name"]])) if sessions else reporter.check(6, "fixed titles return direct playable sources", lambda: unavailable("sign-ins unavailable"))
    delta = reporter.check(7, "64 MiB download is not relayed", lambda: no_relay(args.ssh_host, args.project, playback["movie_source"])) if playback else reporter.check(7, "64 MiB download is not relayed", lambda: unavailable("playable movie source unavailable"))
    if delta is not None:
        reporter.info("AIOStreams TX delta", f"{delta / MIB:.2f} MiB")
    latencies = reporter.check(8, "Succession search returns results within 30s", lambda: search_three_times(base, sessions[primary["name"]])) if sessions else reporter.check(8, "Succession search returns results within 30s", lambda: unavailable("sign-ins unavailable"))
    if latencies:
        reporter.info("search median latency", f"{statistics.median(latencies):.2f}s")
    if sessions and playback:
        reporter.check(9, "fresh persona Next Up starts at S1E1", lambda: next_up_s1e1(base, sessions[test_persona], playback["first_series"]))
        reporter.check(12, "played state survives AIOStreams restart", lambda: durable_write(base, url, args.ssh_host, args.remote_dir, args.project, sessions[test_persona], playback["movie"]))
    else:
        reporter.check(9, "fresh persona Next Up starts at S1E1", lambda: unavailable("playback setup unavailable"))
        reporter.check(12, "played state survives AIOStreams restart", lambda: unavailable("playback setup unavailable"))
    return 1 if reporter.failures else 0


if __name__ == "__main__":
    sys.exit(main())
