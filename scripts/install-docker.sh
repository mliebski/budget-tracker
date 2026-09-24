#!/usr/bin/env bash
# Installs Docker Engine + the Compose plugin from Docker's official apt repository.
# Works on Ubuntu, Debian and Raspberry Pi OS. Run as your normal user (uses sudo).
set -euo pipefail

. /etc/os-release
case "$ID" in
  ubuntu|debian) repo="$ID" ;;
  raspbian) repo="debian" ;;
  *) echo "Unsupported distribution '$ID'. See https://docs.docker.com/engine/install/"; exit 1 ;;
esac

if command -v docker >/dev/null 2>&1; then
  echo "Docker is already installed: $(docker --version)"
  exit 0
fi

sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL "https://download.docker.com/linux/$repo/gpg" -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo
echo "Docker installed: $(docker --version)"
echo "Log out and back in (or run 'newgrp docker') so you can use docker without sudo."
