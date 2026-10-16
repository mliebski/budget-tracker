#!/usr/bin/env bash
# Update the app on the box to the latest version on GitHub.
# Takes a database backup first, then pulls and rebuilds. Your data and .env are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

# git needs your own SSH key for GitHub; under sudo it runs as root, which has none.
if [ "$(id -u)" -eq 0 ]; then
  echo "Run this without sudo: bash scripts/update.sh"
  echo "(git uses your own GitHub key; root doesn't have one. If docker says permission denied,"
  echo " run 'sudo usermod -aG docker \$USER', log out and back in, and try again.)"
  exit 1
fi

if docker compose ps --status running --services 2>/dev/null | grep -qx backup; then
  echo "Taking a backup before updating…"
  docker compose exec -T backup bash /backup.sh now
fi

git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null

echo
echo "Updated to $(git log -1 --format='%h %s (%cd)' --date=short)"
