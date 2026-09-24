#!/usr/bin/env bash
# First-time setup and start of the budget app. Safe to run again (keeps your .env).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker with the compose plugin is required. Run scripts/install-docker.sh first."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=$(openssl rand -hex 24)/" .env
  sed -i "s/^DB_ROOT_PASSWORD=.*/DB_ROOT_PASSWORD=$(openssl rand -hex 24)/" .env
  tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$tz" ] && sed -i "s|^TZ=.*|TZ=$tz|" .env
  echo "Created .env with random database passwords (time zone: ${tz:-America/Toronto})."
fi

set -a; . ./.env; set +a
mkdir -p "${BACKUP_PATH:-./backups}"

docker compose up -d --build

ip="$(hostname -I | awk '{print $1}')"
echo
echo "Budget is starting. Open it from any device on your home network:"
echo "  http://$ip:${APP_PORT:-8000}"
echo "  http://$(hostname).local:${APP_PORT:-8000}   (if your network supports .local names)"
