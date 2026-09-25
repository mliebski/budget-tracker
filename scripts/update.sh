#!/usr/bin/env bash
# Update the app on the box to the latest version on GitHub.
# Takes a database backup first, then pulls and rebuilds. Your data and .env are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

if docker compose ps --status running --services 2>/dev/null | grep -qx backup; then
  echo "Taking a backup before updating…"
  docker compose exec -T backup bash /backup.sh now
fi

git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null

echo
echo "Updated to $(git log -1 --format='%h %s (%cd)' --date=short)"
