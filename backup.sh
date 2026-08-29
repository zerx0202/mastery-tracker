#!/bin/zsh
# Backup bazy mastery-tracker na desktopa przez Tailscale (SFTP).
# Backupujemy WYLACZNIE plik bazy - .env z kluczem API nigdy nie wyjezdza.
set -euo pipefail

export RESTIC_REPOSITORY="sftp:desktop:/C:/backup/mastery"
export RESTIC_PASSWORD_FILE="$HOME/.restic-pass"
export PATH="/opt/homebrew/bin:$PATH"

DB="$HOME/stacks/riot/data/mastery.db"
SNAP="/tmp/mastery-snap.db"

# .backup robi spojna kopie zywej bazy - zwykle cp w trybie WAL potrafi dac plik uszkodzony
sqlite3 "$DB" ".backup '$SNAP'"
sqlite3 "$SNAP" "PRAGMA integrity_check;" | grep -q '^ok$' || { echo "BAZA USZKODZONA - przerywam"; exit 1; }

restic backup "$SNAP" --tag mastery
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune

rm -f "$SNAP"
echo "$(date '+%Y-%m-%d %H:%M') backup ok"
