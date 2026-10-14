#!/usr/bin/env bash
set -euo pipefail

load_compose_command() {
  local stack_dir="${1:?stack directory required}" project_directory="${2:-}" profile
  cd "$stack_dir"
  unset COMPOSE_PROFILES
  eval "$(./ops/env-export.py versions.env)"
  COMPOSE=(docker compose -p "${PROJECT:-media-gateway}")
  if [ -n "$project_directory" ]; then
    COMPOSE+=(--project-directory "$project_directory" --env-file "$stack_dir/.env" --env-file "$stack_dir/versions.env")
  else
    COMPOSE+=(--env-file .env --env-file versions.env)
  fi
  IFS=',' read -r -a compose_profiles <<< "${COMPOSE_PROFILES:-}"
  for profile in "${compose_profiles[@]}"; do
    profile="${profile//[[:space:]]/}"
    if [ -n "$profile" ]; then
      COMPOSE+=(--profile "$profile")
    fi
  done
  if [ -n "$project_directory" ]; then
    COMPOSE+=(-f "$stack_dir/compose.yml")
    if [ "${SMOKE:-0}" = 1 ]; then
      COMPOSE+=(-f "$stack_dir/compose.smoke.yml")
    fi
  else
    COMPOSE+=(-f compose.yml)
    if [ "${SMOKE:-0}" = 1 ]; then
      COMPOSE+=(-f compose.smoke.yml)
    fi
  fi
}

compose_config_sha() {
  local stack_dir="${1:?stack directory required}" project_directory="${2:-}"
  (
    load_compose_command "$stack_dir" "$project_directory"
    "${COMPOSE[@]}" config | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  )
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: compose-config-sha.sh <stack-dir> [project-directory]" >&2
    exit 2
  fi
  compose_config_sha "$1" "${2:-}"
fi
