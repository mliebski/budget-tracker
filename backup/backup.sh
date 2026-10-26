#!/usr/bin/env bash
# Nightly MariaDB dump plus a copy of uploaded files, run inside the "backup" container.
#   docker compose exec backup bash /backup.sh now    # take a backup immediately
set -euo pipefail

BACKUP_TIME="${BACKUP_TIME:-02:30}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
export MYSQL_PWD="$DB_PASSWORD"

run_backup() {
  local ts file tmp
  ts="$(date +%Y-%m-%d_%H%M)"
  file="/backups/budget-$ts.sql.gz"
  tmp="/backups/.budget-$ts.sql.gz.part"
  # Each step is checked by hand: this function runs as "run_backup || ...", where bash
  # ignores set -e, so a failed dump would otherwise be kept as if it were good.
  if ! mariadb-dump -h db -u budget --single-transaction --quick budget | gzip > "$tmp"; then
    rm -f "$tmp"
    echo "$(date '+%F %T') mariadb-dump failed" >&2
    return 1
  fi
  # A finished dump ends with this line; a cut-off one doesn't.
  if ! gzip -t "$tmp" || ! zcat "$tmp" | tail -n 1 | grep -q '^-- Dump completed'; then
    rm -f "$tmp"
    echo "$(date '+%F %T') dump is incomplete or corrupt" >&2
    return 1
  fi
  mv "$tmp" "$file" || return 1
  # Old dumps are only removed after a good one has been written.
  find /backups -maxdepth 1 -name 'budget-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
  echo "$(date '+%F %T') backup written: $(basename "$file") ($(du -h "$file" | cut -f1))"

  # Receipts & documents. Uploaded files never change, so copying new ones is enough.
  # Files deleted in the app are kept here, so an older database dump can still be restored.
  if [ -d /uploads ]; then
    if ! { mkdir -p /backups/files && cp -a --update=none /uploads/. /backups/files/; }; then
      echo "$(date '+%F %T') copying files failed" >&2
      return 1
    fi
    echo "$(date '+%F %T') files copied: $(find /backups/files -type f | wc -l) in total ($(du -sh /backups/files | cut -f1))"
  fi
}

if [ "${1:-}" = "now" ]; then
  run_backup
  exit 0
fi

echo "Nightly backup at $BACKUP_TIME, keeping $KEEP_DAYS days of dumps in /backups"
while true; do
  now="$(date +%s)"
  next="$(date -d "today $BACKUP_TIME" +%s)"
  [ "$next" -le "$now" ] && next="$(date -d "tomorrow $BACKUP_TIME" +%s)"
  sleep "$((next - now))"
  run_backup || echo "$(date '+%F %T') BACKUP FAILED" >&2
done
