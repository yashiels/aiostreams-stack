#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

unit_dir="${HOME}/.config/systemd/user"
mkdir -p "$unit_dir"

changed=0
for source_unit in backup/media-gateway-backup.service backup/media-gateway-backup.timer; do
  unit="$(basename "$source_unit")"
  if ! cmp -s "$source_unit" "${unit_dir}/${unit}"; then
    install -m 644 "$source_unit" "${unit_dir}/${unit}"
    changed=1
  fi
done

for unit in media-gateway-specials-watch.service media-gateway-prune-specials.timer media-gateway-prune-specials.service; do
  if [ -e "${unit_dir}/${unit}" ]; then
    systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "${unit_dir:?}/${unit}"
    changed=1
  fi
done

[ "$changed" -eq 1 ] && systemctl --user daemon-reload

systemctl --user enable --now media-gateway-backup.timer >/dev/null
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

systemctl --user is-enabled media-gateway-backup.timer >/dev/null || {
  echo "backup timer not enabled" >&2
  exit 1
}
echo "backup timer enabled"
