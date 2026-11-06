import argparse
import fcntl
import json
import math
import os
import re
import shlex
import stat
import subprocess
from urllib.parse import urlsplit

from common import Http, RequestError, private_directory

PAGE_SIZE = 900
PROGRESS_LIMIT = 10000
WATCHED_FIELDS = (
    "content_id",
    "content_type",
    "title",
    "season",
    "episode",
    "watched_at",
)
PROGRESS_FIELDS = (
    "content_id",
    "content_type",
    "video_id",
    "season",
    "episode",
    "position",
    "duration",
    "last_watched",
    "progress_key",
)
SOURCE_PROGRAM = r"""
const {DatabaseSync} = require('node:sqlite');
const fs = require('node:fs');
const scope = JSON.parse(fs.readFileSync(0, 'utf8'));
const db = new DatabaseSync('/app/data/db.sqlite', {readOnly:true});
const where = "REPLACE(uuid,'-','') = ? AND persona = '' AND kind IN ('movie','episode')";
db.exec('BEGIN');
const counts = db.prepare(`SELECT COUNT(*) AS total,
 SUM(CASE WHEN played = 1 THEN 1 ELSE 0 END) AS watched,
 SUM(CASE WHEN played = 0 AND position_ms > 0 THEN 1 ELSE 0 END) AS resumable
 FROM watch_state WHERE ${where}`).get(scope.uuid);
const rows = db.prepare(`SELECT item_key,kind,media_type,base_id,season,episode,
 video_id,match_key,position_ms,duration_ms,played,last_played_at,updated_at,origin,snapshot
 FROM watch_state WHERE ${where} AND (played = 1 OR (played = 0 AND position_ms > 0))
 ORDER BY item_key`).all(scope.uuid);
db.exec('COMMIT');
db.close();
process.stdout.write(JSON.stringify({scope:{uuid:scope.uuid,persona:''},counts,rows}));
"""


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def source_milliseconds(value):
    require(
        type(value) in (int, float)
        and value >= 0
        and (type(value) is int or math.isfinite(value)),
        "invalid native milliseconds",
    )
    return int(value)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def native_uuid(value):
    require(
        isinstance(value, str)
        and re.fullmatch(
            r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})",
            value,
        ),
        "native identity must be a primary UUID",
    )
    return value.replace("-", "").lower()


def safe_address(value):
    require(text(value), "invalid service address")
    parsed = urlsplit(value)
    require(
        parsed.scheme in ("http", "https")
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment,
        "invalid service address",
    )
    return value.rstrip("/")


class Nuvio:
    def __init__(self):
        email, password = (
            os.environ.get("NUVIO_EMAIL"),
            os.environ.get("NUVIO_PASSWORD"),
        )
        require(
            text(email) and text(password),
            "NUVIO_EMAIL and NUVIO_PASSWORD are required",
        )
        config = Http("https://nuvio.tv").call("GET", "/config.js")
        require(isinstance(config, bytes), "invalid public configuration")
        match = re.search(r"=\s*(\{.*\})\s*;?\s*$", config.decode(), re.DOTALL)
        require(match is not None, "invalid public configuration")
        settings = json.loads(match[1])
        require(isinstance(settings, dict), "invalid public configuration")
        self.http = Http(safe_address(settings.get("supabaseUrl")))
        require(text(settings.get("supabaseAnonKey")), "invalid public configuration")
        self.headers = {"apikey": settings["supabaseAnonKey"]}
        auth = self.http.call(
            "POST",
            "/auth/v1/token?grant_type=password",
            {"email": email, "password": password},
            headers=self.headers,
            retry_429=False,
        )
        require(
            isinstance(auth, dict) and text(auth.get("access_token")),
            "authentication failed",
        )
        self.headers["Authorization"] = "Bearer " + auth["access_token"]
        user = self.http.call("GET", "/auth/v1/user", headers=self.headers)
        require(
            isinstance(user, dict)
            and isinstance(user.get("email"), str)
            and user["email"].casefold() == email.casefold(),
            "authenticated account mismatch",
        )

    def rpc(self, name, body):
        return self.http.call(
            "POST",
            "/rest/v1/rpc/sync_" + name,
            body,
            headers=self.headers,
            retry_429=False,
        )

    def identity(self, args):
        profiles = self.rpc("pull_profiles", {})
        require(isinstance(profiles, list), "invalid profile list")
        matches = [
            p
            for p in profiles
            if isinstance(p, dict) and p.get("profile_index") == args.profile_id
        ]
        require(
            len(matches) == 1 and matches[0].get("name") == args.profile_name,
            "profile identity mismatch",
        )
        servers = self.rpc("pull_media_servers", {"p_profile_id": args.profile_id})
        require(
            isinstance(servers, list)
            and len(servers) == 1
            and isinstance(servers[0], dict)
            and isinstance(servers[0].get("servers_json"), list),
            "invalid saved native connections",
        )
        connections = [
            c
            for c in servers[0]["servers_json"]
            if isinstance(c, dict)
            and c.get("enabled") is True
            and c.get("provider_id") == "jellyfin"
        ]
        require(len(connections) == 1, "expected one enabled Jellyfin connection")
        connection = connections[0]
        require(
            connection.get("import_watch_state") is True,
            "native watch import is disabled",
        )
        expected_id = native_uuid(connection.get("remote_user_id"))
        require(text(connection.get("token")), "missing native token")
        user = Http(safe_address(connection.get("address"))).call(
            "GET",
            "/Users/Me",
            headers={"X-Emby-Token": connection["token"]},
        )
        require(
            isinstance(user, dict)
            and native_uuid(user.get("Id")) == expected_id
            and user.get("Name") == args.native_user,
            "native user mismatch",
        )
        return matches[0], connection, expected_id

    def pull(self, resource, profile_id):
        rows = []
        seen = set()
        for page in range(1, 10001):
            body = {"p_profile_id": profile_id}
            if resource == "watched_items":
                body.update(p_page=page, p_page_size=PAGE_SIZE)
            else:
                body["p_limit"] = PROGRESS_LIMIT
            batch = self.rpc("pull_" + resource, body)
            limit = PAGE_SIZE if resource == "watched_items" else PROGRESS_LIMIT
            require(
                isinstance(batch, list) and len(batch) <= limit, "invalid cloud page"
            )
            for row in batch:
                key = identity(row)
                require(key not in seen, "duplicate cloud identity")
                seen.add(key)
            rows.extend(batch)
            if resource == "watch_progress":
                require(len(batch) < limit, "cloud progress may be truncated")
                break
            if len(batch) < limit:
                break
        else:
            raise SystemExit("cloud history pagination did not terminate")
        return cloud_rows(rows, resource)


def identity(row):
    require(isinstance(row, dict), "invalid media row")
    content, kind = row.get("content_id"), row.get("content_type")
    require(text(content) and kind in ("movie", "series"), "invalid media identity")
    season, episode = row.get("season"), row.get("episode")
    require(
        (kind == "movie" and season is None and episode is None)
        or (kind == "series" and integer(season) and integer(episode, 1)),
        "invalid episode identity",
    )
    return content, kind, season, episode


def progress_key(row):
    return (
        row["content_id"]
        if row["content_type"] == "movie"
        else f"{row['content_id']}_s{row['season']}e{row['episode']}"
    )


def cloud_rows(rows, resource):
    fields = WATCHED_FIELDS if resource == "watched_items" else PROGRESS_FIELDS
    result = {}
    keys = set()
    for row in rows:
        key = identity(row)
        require(
            key not in result and all(f in row for f in fields),
            "duplicate or incomplete cloud row",
        )
        normalized = {f: row[f] for f in fields}
        if resource == "watched_items":
            require(
                text(row["title"]) and integer(row["watched_at"], 1),
                "invalid cloud history",
            )
        else:
            require(
                all(integer(row[f]) for f in ("position", "duration"))
                and integer(row["last_watched"], 1)
                and (row["video_id"] is None or text(row["video_id"]))
                and text(row["progress_key"])
                and row["progress_key"] not in keys,
                "invalid cloud progress",
            )
            keys.add(row["progress_key"])
        result[key] = normalized
    return result


def read_source(args, scope):
    remote = shlex.join(
        [
            "docker",
            "exec",
            "-i",
            args.container,
            "/nodejs/bin/node",
            "-e",
            SOURCE_PROGRAM,
        ]
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "--",
            args.ssh_host,
            remote,
        ],
        input=json.dumps({"uuid": scope}),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(result.returncode == 0, "native export failed")
    return source_rows(json.loads(result.stdout), scope)


def source_rows(export, scope):
    require(
        isinstance(export, dict)
        and export.get("scope") == {"uuid": scope, "persona": ""},
        "native scope mismatch",
    )
    counts, rows = export.get("counts"), export.get("rows")
    require(
        isinstance(counts, dict)
        and isinstance(rows, list)
        and all(integer(counts.get(k)) for k in ("total", "watched", "resumable"))
        and counts["total"] > 0
        and counts["total"] >= counts["watched"] + counts["resumable"] == len(rows),
        "incomplete native export",
    )
    result = {}
    watched = 0
    for row in rows:
        require(isinstance(row, dict), "invalid native row")
        position = source_milliseconds(row.get("position_ms"))
        duration = source_milliseconds(row.get("duration_ms"))
        require(
            row.get("kind") in ("movie", "episode")
            and text(row.get("base_id"))
            and re.fullmatch(r"(?:tmdb:[1-9][0-9]*|tt[0-9]+)", row["base_id"])
            and type(row.get("played")) is int
            and row["played"] in (0, 1)
            and integer(row.get("updated_at"))
            and integer(row.get("last_played_at"), 1)
            and (row["played"] == 1 or row["position_ms"] > 0),
            "invalid native watch state",
        )
        item = {
            "content_id": row["base_id"],
            "content_type": "movie" if row["kind"] == "movie" else "series",
            "season": row.get("season"),
            "episode": row.get("episode"),
        }
        key = identity(item)
        snapshot = json.loads(row["snapshot"])
        require(isinstance(snapshot, dict), "invalid native snapshot")
        title = snapshot.get("seriesName") if row["kind"] == "episode" else None
        title = title or snapshot.get("name")
        require(text(title), "missing native title")
        runtime = (
            source_milliseconds(snapshot["runtimeMs"]) if "runtimeMs" in snapshot else 0
        )
        duration = duration or runtime
        video_id = row.get("video_id")
        require(video_id is None or text(video_id), "invalid native video identity")
        require(
            row["kind"] == "movie" or text(video_id), "missing native video identity"
        )
        item.update(
            title=title,
            watched_at=row["last_played_at"],
            played=row["played"],
            video_id=video_id or row["base_id"],
            position=position,
            duration=duration,
            last_watched=row["last_played_at"],
        )
        item["progress_key"] = progress_key(item)
        watched += row["played"]
        if key not in result or item["last_watched"] > result[key]["last_watched"]:
            result[key] = item
    require(
        watched == counts["watched"] and len(rows) - watched == counts["resumable"],
        "native count mismatch",
    )
    return result


def merge(source, watched, progress):
    desired_watched = {k: dict(v) for k, v in watched.items()}
    desired_progress = {k: dict(v) for k, v in progress.items()}
    for key, row in source.items():
        if row["played"]:
            if key not in watched or row["watched_at"] > watched[key]["watched_at"]:
                desired_watched[key] = {f: row[f] for f in WATCHED_FIELDS}
            old = progress.get(key)
            if old and row["last_watched"] > old["last_watched"]:
                duration = row["duration"] or old["duration"]
                if duration > 0:
                    desired_progress[key] = {
                        **old,
                        "position": duration,
                        "duration": duration,
                        "last_watched": row["last_watched"],
                    }
        elif key not in progress or row["last_watched"] > progress[key]["last_watched"]:
            desired_progress[key] = {f: row[f] for f in PROGRESS_FIELDS}
    cloud_rows(list(desired_progress.values()), "watch_progress")
    return desired_watched, desired_progress


def synchronize(client, args, original_identity, source):
    resources = ("watched_items", "watch_progress")
    before = tuple(client.pull(r, args.profile_id) for r in resources)
    desired = merge(source, *before)
    changes = [
        sum(old.get(k) != v for k, v in new.items())
        for old, new in zip(before, desired)
    ]
    unknown = sum(not row["played"] and row["duration"] == 0 for row in source.values())
    print(f"source complete: {len(source)} selected; unknown duration: {unknown}")
    print(f"cloud: watched {len(before[0])}; progress {len(before[1])}")
    print(f"planned: watched {changes[0]}; progress {changes[1]}")
    if not args.apply or not any(changes):
        print("no changes" if not any(changes) else "dry-run: no writes")
        return
    for index, resource in enumerate(resources):
        if not changes[index]:
            continue
        require(
            client.identity(args) == original_identity,
            "profile or native connection drift",
        )
        require(
            tuple(client.pull(r, args.profile_id) for r in resources) == before,
            "cloud state drift before write",
        )
        uncertain = False
        try:
            client.rpc(
                "push_" + resource,
                {
                    "p_profile_id": args.profile_id,
                    "p_items" if index == 0 else "p_entries": list(
                        desired[index].values()
                    ),
                    "p_origin_client_id": "native-watch-sync",
                },
            )
        except RequestError:
            uncertain = True
        actual = client.pull(resource, args.profile_id)
        require(actual == desired[index], "write readback mismatch; no retry attempted")
        before = tuple(actual if i == index else old for i, old in enumerate(before))
        print(
            resource
            + (": uncertain write reconciled" if uncertain else ": write verified")
        )
    require(
        tuple(client.pull(r, args.profile_id) for r in resources) == desired,
        "final readback mismatch",
    )
    print("apply verified")


def main():
    parser = argparse.ArgumentParser(
        description="Add/update primary native watch state in Nuvio. Default: dry-run."
    )
    parser.add_argument(
        "--profile-id", type=int, required=True, help="Existing numeric profile index"
    )
    parser.add_argument("--profile-name", required=True)
    parser.add_argument("--native-user", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Private local directory for the overlap lock",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Attempt each changed resource once and verify readback",
    )
    args = parser.parse_args()
    require(
        args.profile_id >= 0
        and all(
            text(getattr(args, f))
            for f in (
                "profile_name",
                "native_user",
                "ssh_host",
                "container",
            )
        ),
        "explicit nonempty identities are required",
    )
    require(
        not args.ssh_host.startswith("-") and not args.container.startswith("-"),
        "invalid remote target",
    )
    directory = private_directory(args.state_dir)
    descriptor = os.open(
        directory / "nuvio-watch-sync.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as lock:
        metadata = os.fstat(lock.fileno())
        require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not metadata.st_mode & 0o077,
            "invalid local lock",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another sync is running") from None
        client = Nuvio()
        selected = client.identity(args)
        source = read_source(args, selected[2])
        synchronize(client, args, selected, source)


if __name__ == "__main__":
    try:
        main()
    except (
        RequestError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        raise SystemExit("sync failed; no automatic mutation retry") from None
