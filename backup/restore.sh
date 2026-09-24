#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=ops/with-lock.sh
. ./ops/with-lock.sh
take_lock "$@"

SNAPSHOT="${1:-latest}"

eval "$(./ops/env-export.py .env)"

: "${RESTIC_REPOSITORY:?}"
: "${RESTIC_PASSWORD:?}"

COMPOSE=(docker compose -p "${PROJECT:-media-gateway}" --env-file .env --env-file versions.env -f compose.yml)

restic snapshots "$SNAPSHOT" >/dev/null

staging="$(mktemp -d ./restore.XXXXXX)"
cleanup_staging() { rm -rf "$staging"; }
trap cleanup_staging EXIT

restic restore "$SNAPSHOT" --target "$staging"

restored="${staging}$(pwd)"
[ -d "${restored}/data/aiostreams" ] || {
  echo "snapshot is missing data/aiostreams" >&2
  exit 1
}

stamp="$(date +%Y%m%d-%H%M%S)"
rollback_dir="restore-rollback/${stamp}"
mkdir -p "$rollback_dir"
aiostreams_saved="${rollback_dir}/aiostreams"
aiostreams_existed=0
[ -d data/aiostreams ] && aiostreams_existed=1

restore_done=0
recover() {
  if [ "$restore_done" -eq 0 ]; then
    echo "restore failed, rolling back to pre-restore state" >&2
    "${COMPOSE[@]}" down || true
    if [ "$aiostreams_existed" -eq 1 ]; then
      [ -d "$aiostreams_saved" ] && { rm -rf data/aiostreams; mv "$aiostreams_saved" data/aiostreams; }
    else
      rm -rf data/aiostreams
    fi
    "${COMPOSE[@]}" up -d || true
  fi
  cleanup_staging
}
trap recover EXIT

"${COMPOSE[@]}" down

[ -d data/aiostreams ] && mv data/aiostreams "$aiostreams_saved"
mv "${restored}/data/aiostreams" data/aiostreams

./ops/deploy.sh

restore_done=1
trap - EXIT
cleanup_staging

echo "pre-restore state kept in ${rollback_dir} (outside data/, delete once verified)"
