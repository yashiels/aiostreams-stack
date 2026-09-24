#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -ne 0 ] || { echo "run as the deploy user with sudo, not as root" >&2; exit 1; }
sudo -v

# shellcheck disable=SC1091
. /etc/os-release
case "$ID" in
  debian|ubuntu) distro="$ID" ;;
  *) case "${ID_LIKE:-}" in *debian*) distro=debian ;; *) echo "unsupported distro: $ID" >&2; exit 1 ;; esac ;;
esac

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gnupg restic rsync sqlite3 unzip python3

if ! command -v docker >/dev/null; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL "https://download.docker.com/linux/${distro}/gpg" -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${distro} ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

deploy_user="$(id -un)"
sudo usermod -aG docker "$deploy_user"
sudo loginctl enable-linger "$deploy_user"
install -d "$HOME/apps/media-gateway"

cat <<NEXT
=== provisioned ===
docker: $(docker --version 2>/dev/null || echo "installed; log out and back in to pick up the docker group")

versions.env is set for:
  MEDIA_UID=$(id -u)
  MEDIA_GID=$(id -g)

Next steps are in README.md, "Quickstart".
NEXT
