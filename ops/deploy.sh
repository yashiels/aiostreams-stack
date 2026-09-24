#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=ops/with-lock.sh
. ./ops/with-lock.sh

# shellcheck source=ops/compose-config-sha.sh
. ./ops/compose-config-sha.sh

ORIGINAL_ARGS=("$@")
NO_TUNNEL=0
PRINT_CONFIG_SHA=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-tunnel) NO_TUNNEL=1 ;;
    --print-config-sha) PRINT_CONFIG_SHA=1 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

MIN_FREE_GB=10

[ -f .env ] || {
  echo "no .env on this host — render it once:" >&2
  echo "  make env" >&2
  exit 1
}

./ops/env-check.py .env >/dev/null
compose_sha="$(compose_config_sha "$PWD")"
[ "$PRINT_CONFIG_SHA" -eq 0 ] || { printf '%s\n' "$compose_sha"; exit 0; }
if [ -n "${EXPECTED_COMPOSE_SHA256:-}" ] && [ "$compose_sha" != "$EXPECTED_COMPOSE_SHA256" ]; then
  echo "compose config sha256 mismatch: expected ${EXPECTED_COMPOSE_SHA256}, got ${compose_sha}" >&2
  exit 1
fi

take_lock "${ORIGINAL_ARGS[@]}"
load_compose_command "$PWD"
tmdb_enabled=0
for profile in "${compose_profiles[@]}"; do
  [ "${profile//[[:space:]]/}" = tmdb ] && tmdb_enabled=1
done

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  echo "refusing to deploy: ${free_gb}G free on /, need ${MIN_FREE_GB}G" >&2
  exit 1
fi

mkdir -p data/aiostreams
[ "$(id -u):$(id -g)" = "${MEDIA_UID}:${MEDIA_GID}" ] || {
  echo "versions.env MEDIA_UID:MEDIA_GID ${MEDIA_UID}:${MEDIA_GID} != deploy user $(id -u):$(id -g)" >&2
  exit 1
}

"${COMPOSE[@]}" pull

if [ -f data/aiostreams/db.sqlite ]; then
  ./backup/backup.sh --pre-upgrade
fi

./ops/install-timer.sh

enabled_services=(aiostreams cloudflared)
[ "$tmdb_enabled" -eq 1 ] && enabled_services=(aiostreams tmdb-addon cloudflared)

started_services=()
for service in "${enabled_services[@]}"; do
  if [ "$NO_TUNNEL" -eq 0 ] || [ "$service" != cloudflared ]; then
    started_services+=("$service")
  fi
done
[ "${#started_services[@]}" -gt 0 ] || { echo "no services selected" >&2; exit 1; }

if [ "$NO_TUNNEL" -eq 1 ]; then
  "${COMPOSE[@]}" stop cloudflared >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps "${started_services[@]}"
else
  "${COMPOSE[@]}" up -d
fi

echo "=== deployed ==="
"${COMPOSE[@]}" ps

health_services=(aiostreams)
for service in "${started_services[@]}"; do
  [ "$service" = tmdb-addon ] && health_services+=(tmdb-addon)
done

wait_healthy() {
  local deadline=$((SECONDS + ${1:-600})) service container state pending
  while :; do
    pending=""
    for service in "${health_services[@]}"; do
      container="$("${COMPOSE[@]}" ps -q "$service")"
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo missing)"
      case "$state" in
        no-healthcheck|missing) echo "$service: $state, cannot gate on health" >&2; return 1 ;;
      esac
      [ "$state" = healthy ] || pending+="$service=$state "
    done
    [ -z "$pending" ] && { echo "healthy: ${health_services[*]}"; return 0; }
    [ "$SECONDS" -ge "$deadline" ] && { echo "not healthy after wait: $pending" >&2; return 1; }
    sleep 10
  done
}

wait_healthy 900

if ! ( eval "$(./ops/env-export.py .env)"; restic snapshots --tag media-gateway --latest 1 --json 2>/dev/null | grep -q '"time"' ); then
  echo "no baseline snapshot yet, taking one"
  ./backup/backup.sh
  wait_healthy 900
fi

if [ "$NO_TUNNEL" -eq 1 ]; then
  ./ops/probe.sh --internal
else
  ./ops/probe.sh
fi
