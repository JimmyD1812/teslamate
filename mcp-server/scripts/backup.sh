#!/bin/sh
# Nightly TeslaMate database backup with simple rotation.
#
# MyTeslaMate's headline convenience is "hosted and automatically backed
# up". On your own hardware this script is the equivalent: it dumps the
# TeslaMate PostgreSQL database (from inside its container) to a dated,
# gzipped file and keeps the most recent N dumps.
#
# Run it from the directory that holds your TeslaMate docker-compose.yml,
# e.g. from cron on the host:
#
#   15 3 * * *  cd /path/to/teslamate && ./mcp-server/scripts/backup.sh
#
# Optional environment:
#   TESLAMATE_BACKUP_DIR   where dumps go     (default: ~/teslamate-backups)
#   TESLAMATE_BACKUP_KEEP  how many to keep   (default: 14)
#   TESLAMATE_DB_USER      database owner     (default: teslamate)
#   TESLAMATE_DB_NAME      database name      (default: teslamate)

set -eu

BACKUP_DIR="${TESLAMATE_BACKUP_DIR:-$HOME/teslamate-backups}"
KEEP="${TESLAMATE_BACKUP_KEEP:-14}"
DB_USER="${TESLAMATE_DB_USER:-teslamate}"
DB_NAME="${TESLAMATE_DB_NAME:-teslamate}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/teslamate-$STAMP.sql.gz"

docker compose exec -T database pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"

# Rotation: delete all but the KEEP newest dumps.
ls -1t "$BACKUP_DIR"/teslamate-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done

echo "Backup written: $OUT"
