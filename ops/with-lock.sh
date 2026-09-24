#!/usr/bin/env bash

MEDIA_GATEWAY_LOCKFILE="${MEDIA_GATEWAY_LOCKFILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.stack.lock}"

take_lock() {
  [ "${MEDIA_GATEWAY_LOCK_HELD:-0}" = "1" ] && return 0
  export MEDIA_GATEWAY_LOCK_HELD=1
  exec flock --timeout "${MEDIA_GATEWAY_LOCK_WAIT:-1800}" "$MEDIA_GATEWAY_LOCKFILE" "$0" "$@"
}
