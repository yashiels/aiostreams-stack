#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
if [ "${1:-}" = --help ]; then
  echo 'usage: each-config.sh ops/COMMAND.py [ARGUMENTS...]'
  exit 0
fi
pause=${EACH_CONFIG_PAUSE-40}
if [[ ! "$pause" =~ ^[0-9]{2,3}$ ]] || ((10#$pause < 30 || 10#$pause > 600)); then
  echo 'EACH_CONFIG_PAUSE must be an integer from 30 to 600' >&2
  exit 2
fi
if [ "$#" -eq 0 ] || [[ ! "$1" =~ ^ops/[a-z-]+\.py$ ]] || [ ! -f "$1" ]; then
  echo 'usage: each-config.sh ops/COMMAND.py [ARGUMENTS...]' >&2
  exit 2
fi
for arg in "$@"; do
  if [[ "$arg" = --config || "$arg" = --config=* ]]; then
    echo 'each-config.sh selects the config name' >&2
    exit 2
  fi
done
names=$(python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "ops"))
from common import load_configs
configs = load_configs(Path.cwd())
print("main")
for config in configs:
    print(config["name"])
PY
)
first=1
while IFS= read -r name; do
  if [ "$first" -eq 0 ]; then
    sleep "$pause"
  fi
  first=0
  if ! python3 "$@" --config "$name"; then
    printf 'config %s failed; stopped\n' "$name" >&2
    exit 1
  fi
done <<< "$names"
