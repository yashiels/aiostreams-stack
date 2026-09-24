#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

usage() {
  echo "usage: smoke.sh <env|up|down> <smoke-dir> <port>" >&2
  exit 2
}

[ "$#" -eq 3 ] || usage
action="$1"
smoke_dir="$2"
smoke_port="$3"

smoke_suffix="${smoke_dir#.smoke}"
smoke_suffix="${smoke_suffix#-}"
if [ "$smoke_dir" != .smoke ]; then
  case "$smoke_dir" in
    .smoke-?*) ;;
    *) echo "smoke dir must be .smoke or .smoke-<name>" >&2; exit 1 ;;
  esac
  case "$smoke_suffix" in
    *[!A-Za-z0-9_-]*) echo "smoke dir suffix must use only [A-Za-z0-9_-]" >&2; exit 1 ;;
  esac
fi
case "$smoke_port" in
  '' | *[!0-9]*) echo "smoke port must be numeric" >&2; exit 1 ;;
esac
if [ "$smoke_port" -lt 1024 ] || [ "$smoke_port" -gt 65535 ]; then
  echo "smoke port must be 1024-65535" >&2
  exit 1
fi

smoke_compose() {
  (
    cd "$smoke_dir"
    COMPOSE_PROFILES='' SMOKE_PORT="$smoke_port" docker compose -p aiostreams-smoke \
      --env-file .env --env-file versions.env -f compose.yml -f compose.smoke.yml "$@"
  )
}

smoke_teardown() {
  if [ -f "$smoke_dir/compose.yml" ]; then
    smoke_compose down -v
  fi
  if [ -d "$smoke_dir/data" ]; then
    docker run --rm -v "$PWD/$smoke_dir:/smoke" --entrypoint /bin/sh alpine:3.20 -c 'rm -rf /smoke/data'
  fi
  rm -rf -- "./$smoke_dir"
}

random_token() {
  python3 -c "import secrets, sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"
}

random_hex_key() {
  python3 -c "import secrets; print(secrets.token_hex(32))"
}

smoke_env() {
  [ -f .env ] || { echo "local .env is required for smoke credentials" >&2; exit 1; }
  smoke_teardown
  mkdir -p "$smoke_dir/ops"
  cp compose.yml compose.smoke.yml versions.env "$smoke_dir/"
  cp ops/env-export.py "$smoke_dir/ops/"
  local torbox tmdb secret_key auth_password access_key line
  torbox="$(eval "$(./ops/env-export.py .env)"; printf '%s' "${TORBOX_API_KEY:-}")"
  tmdb="$(eval "$(./ops/env-export.py .env)"; printf '%s' "${TMDB_API_KEY:-}")"
  [ -n "$torbox" ] || { echo "TORBOX_API_KEY missing from .env" >&2; exit 1; }
  [ -n "$tmdb" ] || { echo "TMDB_API_KEY missing from .env" >&2; exit 1; }
  secret_key="$(random_hex_key)"
  auth_password="$(random_token 32)"
  access_key="$(random_token 48)"
  (
    umask 077
    while IFS= read -r line; do
      case "$line" in
        AIOSTREAMS_PUBLIC_URL=*) printf 'AIOSTREAMS_PUBLIC_URL=http://127.0.0.1:%s\n' "$smoke_port" ;;
        STACK_PUBLIC_URL=*) printf 'STACK_PUBLIC_URL=http://127.0.0.1:%s\n' "$smoke_port" ;;
        AIOSTREAMS_SECRET_KEY=*) printf 'AIOSTREAMS_SECRET_KEY=%s\n' "$secret_key" ;;
        AIOSTREAMS_AUTH=*) printf 'AIOSTREAMS_AUTH=admin:%s\n' "$auth_password" ;;
        AIOSTREAMS_CONFIG_ACCESS_KEY=*) printf 'AIOSTREAMS_CONFIG_ACCESS_KEY=%s\n' "$access_key" ;;
        TORBOX_API_KEY=*) printf 'TORBOX_API_KEY=%s\n' "$torbox" ;;
        TMDB_API_KEY=*) printf 'TMDB_API_KEY=%s\n' "$tmdb" ;;
        MEDIA_GATEWAY_TUNNEL_TOKEN=*) printf 'MEDIA_GATEWAY_TUNNEL_TOKEN=unused-in-smoke\n' ;;
        RESTIC_REPOSITORY=* | RESTIC_PASSWORD=* | AWS_ACCESS_KEY_ID=* | AWS_SECRET_ACCESS_KEY=*) printf '%s\n' "${line%%=*}=" ;;
        AIOSTREAMS_UUID=* | AIOSTREAMS_PASSWORD=*) printf '%s\n' "${line%%=*}=" ;;
        COMPOSE_PROFILES=*) ;;
        *) printf '%s\n' "$line" ;;
      esac
    done < .env.example > "$smoke_dir/.env"
  )
}

smoke_up() {
  smoke_env
  local services containers attempt state
  services="$(smoke_compose config --services)"
  [ "$services" = aiostreams ] || { echo "smoke config must contain only aiostreams, got: $services" >&2; exit 1; }
  smoke_compose up -d
  containers="$(docker ps --filter label=com.docker.compose.project=aiostreams-smoke --format '{{.Names}}|{{.Ports}}')"
  [ "$containers" = "aiostreams-smoke-aiostreams-1|127.0.0.1:${smoke_port}->3000/tcp" ] || {
    echo "unexpected smoke containers or ports: $containers" >&2
    exit 1
  }
  for attempt in $(seq 90); do
    state="$(docker inspect -f '{{.State.Status}}/{{.State.Health.Status}}' aiostreams-smoke-aiostreams-1 2>/dev/null || true)"
    [ "$state" = running/healthy ] && break
    case "$state" in
      restarting/* | exited/* | dead/*)
        echo "smoke service failed to start ($state):" >&2
        docker logs --tail 20 aiostreams-smoke-aiostreams-1 >&2 || true
        exit 1
        ;;
    esac
    [ "$attempt" -lt 90 ] || { echo "smoke service did not become healthy" >&2; exit 1; }
    sleep 10
  done
  (
    eval "$(./ops/env-export.py "$smoke_dir/.env")"
    export AIOSTREAMS_UUID='' AIOSTREAMS_PASSWORD=''
    export AIOSTREAMS_PUBLIC_URL="http://127.0.0.1:${smoke_port}"
    python3 ops/bootstrap.py --url "http://127.0.0.1:${smoke_port}"
  )
}

case "$action" in
  env) smoke_env ;;
  up) smoke_up ;;
  down) smoke_teardown ;;
  *) usage ;;
esac
