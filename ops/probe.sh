#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

eval "$(./ops/env-export.py versions.env)"
[ -f .env ] && eval "$(./ops/env-export.py .env)"

AIOSTREAMS_URL="${AIOSTREAMS_PUBLIC_URL:-https://aio.example.com}"
MIN_FREE_GB="${MIN_FREE_GB:-6}"

fail=0
verbose=0
internal=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --verbose) verbose=1 ;;
    --internal) internal=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

tmdb_enabled=0
IFS=',' read -r -a profiles <<< "${COMPOSE_PROFILES:-}"
COMPOSE=(docker compose -p "${PROJECT:-media-gateway}" --env-file .env --env-file versions.env)
for profile in "${profiles[@]}"; do
  profile="${profile//[[:space:]]/}"
  [ -n "$profile" ] && COMPOSE+=(--profile "$profile")
  [ "$profile" = tmdb ] && tmdb_enabled=1
done
COMPOSE+=(-f compose.yml)

check() {
  local name="$1"
  shift
  local out
  if out="$("$@" 2>&1)"; then
    [ "$verbose" -eq 1 ] && echo "ok   ${name}"
    return 0
  fi
  echo "FAIL ${name}"
  [ "$verbose" -eq 1 ] && [ -n "$out" ] && echo "     ${out}"
  fail=$((fail + 1))
}

containers_running() {
  local count expected
  expected=2
  [ "$tmdb_enabled" -eq 1 ] && expected=$((expected + 1))
  [ "$internal" -eq 1 ] && expected=$((expected - 1))
  count="$("${COMPOSE[@]}" ps --status running --format '{{.Service}}' | sort -u | wc -l | tr -d ' ')"
  [ "$count" -eq "$expected" ] || { echo "only ${count}/${expected} services running"; return 1; }
}

service_healthy() {
  local service="$1" container state
  container="$("${COMPOSE[@]}" ps -q "$service")"
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo missing)"
  [ "$state" = healthy ] || { echo "container health is ${state}"; return 1; }
}

aiostreams_internal_jellyfin() {
  "${COMPOSE[@]}" exec -T aiostreams /nodejs/bin/node -e "fetch('http://127.0.0.1:3000/jellyfin/System/Info/Public').then(async r=>{const j=await r.json();process.exit(r.ok&&j.ProductName==='Jellyfin Server'?0:1)}).catch(()=>process.exit(1))"
}

tmdb_internal_manifest() {
  "${COMPOSE[@]}" exec -T aiostreams /nodejs/bin/node -e "fetch('http://tmdb-addon:1337/manifest.json').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
}

aiostreams_public_jellyfin() {
  local attempt body
  for attempt in $(seq 6); do
    body="$(curl -fsS --max-time 15 -H 'User-Agent: curl/8.7.1' "${AIOSTREAMS_URL}/jellyfin/System/Info/Public" 2>/dev/null)" \
      && printf '%s' "$body" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ProductName") == "Jellyfin Server" else 1)' \
      && return 0
    sleep 10
  done
  echo "no Jellyfin Server response after ${attempt} attempts"
  return 1
}

aiostreams_public_manifest() {
  local attempt
  for attempt in $(seq 6); do
    curl -fsS --max-time 15 "${AIOSTREAMS_URL}/api/v1/health" >/dev/null 2>&1 \
      && curl -fsS --max-time 15 "${AIOSTREAMS_URL}/stremio/manifest.json" 2>/dev/null | grep -q '"id"' \
      && return 0
    sleep 10
  done
  echo "no manifest after ${attempt} attempts"
  return 1
}

disk_headroom() {
  local free_gb
  free_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
  [ -n "$free_gb" ] || { echo "cannot read free space"; return 1; }
  [ "$free_gb" -ge "$MIN_FREE_GB" ] || { echo "${free_gb}G free, want >= ${MIN_FREE_GB}G"; return 1; }
}

backup_timer_active() {
  systemctl --user is-active media-gateway-backup.timer >/dev/null \
    || { echo "backup timer not active"; return 1; }
}

backup_is_fresh() {
  local last
  last="$(restic snapshots --tag media-gateway --latest 1 --json 2>/dev/null \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d[0]["time"] if d else "")')" || return 1
  [ -n "$last" ] || { echo "no snapshots"; return 1; }
  python3 - "$last" <<'PY'
import datetime, re, sys
stamp = re.sub(r'(\.\d{6})\d+', r'\1', sys.argv[1]).replace('Z', '+00:00')
t = datetime.datetime.fromisoformat(stamp)
if t.tzinfo is None:
    t = t.replace(tzinfo=datetime.timezone.utc)
age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
print(f"last backup {age:.0f}h ago")
raise SystemExit(0 if age < 48 else 1)
PY
}

check "containers running" containers_running
check "aiostreams container healthy" service_healthy aiostreams
check "aiostreams internal Jellyfin endpoint" aiostreams_internal_jellyfin
if [ "$tmdb_enabled" -eq 1 ]; then
  check "tmdb-addon internal manifest" tmdb_internal_manifest
fi
if [ "$internal" -eq 1 ]; then
  echo "SKIP aiostreams public Jellyfin endpoint (--internal)"
  echo "SKIP aiostreams public manifest (--internal)"
else
  check "aiostreams public Jellyfin endpoint" aiostreams_public_jellyfin
  check "aiostreams public manifest" aiostreams_public_manifest
fi
check "host disk headroom" disk_headroom
check "backup timer active" backup_timer_active
check "backup fresh" backup_is_fresh

if [ "$fail" -gt 0 ]; then
  echo "media-gateway: ${fail} check(s) failed"
  exit 1
fi
echo "media-gateway: all checks passed"
