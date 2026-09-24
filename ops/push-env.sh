#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

host="${1:?usage: push-env.sh <user@host> <remote-dir>}"
remote_dir_input="${2:?usage: push-env.sh <user@host> <remote-dir>}"
ssh_opts=(-o StrictHostKeyChecking=yes)

case "$remote_dir_input" in
  *[!A-Za-z0-9._/~-]*) echo "refusing unsafe remote dir: $remote_dir_input" >&2; exit 1 ;;
esac

[ -f .env ] || { echo "local .env is missing" >&2; exit 1; }
./ops/env-check.py .env
# shellcheck disable=SC2029
remote_dir="$(ssh "${ssh_opts[@]}" "$host" "cd ${remote_dir_input} && pwd")"
[ -n "$remote_dir" ] || { echo "cannot resolve ${remote_dir_input} on ${host}" >&2; exit 1; }

# shellcheck disable=SC2029
ssh "${ssh_opts[@]}" "$host" \
  "umask 077 && cat > '${remote_dir}/.env.incoming' && mv '${remote_dir}/.env.incoming' '${remote_dir}/.env'" < .env

echo "wrote ${host}:${remote_dir}/.env"
