#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=ops/with-lock.sh
. ./ops/with-lock.sh
take_lock "$@"

PRE_UPGRADE=0
[ "${1:-}" = "--pre-upgrade" ] && PRE_UPGRADE=1

eval "$(./ops/env-export.py .env)"

: "${RESTIC_REPOSITORY:?}"
: "${RESTIC_PASSWORD:?}"

COMPOSE=(docker compose -p "${PROJECT:-media-gateway}" --env-file .env --env-file versions.env -f compose.yml)

restic unlock >/dev/null 2>&1 || true
restic snapshots >/dev/null 2>&1 || restic init

running=0
if "${COMPOSE[@]}" ps --status running --format '{{.Service}}' | grep -qx aiostreams; then
  running=1
fi

restart_stateful() {
  [ "$running" -eq 1 ] || return 0
  "${COMPOSE[@]}" start aiostreams
}
recover_stateful() {
  if [ "$running" -eq 1 ]; then
    "${COMPOSE[@]}" start aiostreams >/dev/null 2>&1 || true
  fi
}
trap recover_stateful EXIT

[ "$running" -eq 1 ] && "${COMPOSE[@]}" stop aiostreams

tag="media-gateway"
[ "$PRE_UPGRADE" -eq 1 ] && tag="media-gateway-pre-upgrade"

restic backup --tag "$tag" data/aiostreams

restart_stateful
trap - EXIT

if [ "$PRE_UPGRADE" -eq 0 ]; then
  restic forget --tag media-gateway --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
else
  restic forget --tag media-gateway-pre-upgrade --keep-last 5 --prune
fi
