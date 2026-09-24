#!/usr/bin/env bash
set -euo pipefail

load_compose_command() {
  local stack_dir="${1:?stack directory required}" profile
  cd "$stack_dir"
  unset COMPOSE_PROFILES
  eval "$(./ops/env-export.py versions.env)"
  COMPOSE=(docker compose -p "${PROJECT:-media-gateway}" --env-file .env --env-file versions.env)
  IFS=',' read -r -a compose_profiles <<< "${COMPOSE_PROFILES:-}"
  for profile in "${compose_profiles[@]}"; do
    profile="${profile//[[:space:]]/}"
    if [ -n "$profile" ]; then
      COMPOSE+=(--profile "$profile")
    fi
  done
  COMPOSE+=(-f compose.yml)
  if [ "${SMOKE:-0}" = 1 ]; then
    COMPOSE+=(-f compose.smoke.yml)
  fi
}

compose_config_sha() {
  local stack_dir="${1:?stack directory required}"
  (
    load_compose_command "$stack_dir"
    "${COMPOSE[@]}" config | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  )
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ "$#" -eq 1 ] || { echo "usage: compose-config-sha.sh <stack-dir>" >&2; exit 2; }
  compose_config_sha "$1"
fi
