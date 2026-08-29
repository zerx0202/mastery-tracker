#!/bin/zsh
# Backup bazy mastery-tracker na desktopa przez Tailscale (SFTP).
# Backupujemy WYLACZNIE plik bazy - .env z kluczem API nigdy nie wyjezdza.
set -euo pipefail

export RESTIC_REPOSITORY="sftp:desktop:/C:/backup/mastery"
export RESTIC_PASSWORD_FILE="$HOME/.restic-pass"
export PATH="/opt/homebrew/bin:$PATH"

SNAP="/tmp/mastery-snap.db"

# Kopie robi kontener, a nie host: baza jest na bind moncie przez virtiofs
# i dwa procesy z roznych stron montu potrafia sie o nia pobic.
cd "$HOME/stacks/riot"
/opt/homebrew/bin/docker compose exec -T backend \
  python -c "import sqlite3; s=sqlite3.connect('/code/data/mastery.db'); d=sqlite3.connect('/code/data/_snap.db'); s.backup(d); d.close(); s.close()"
mv "$HOME/stacks/riot/data/_snap.db" "$SNAP"
sqlite3 "$SNAP" "PRAGMA integrity_check;" | grep -q '^ok$' || { echo "BAZA USZKODZONA - przerywam"; exit 1; }

restic backup "$SNAP" --tag mastery
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune

rm -f "$SNAP"
echo "$(date '+%Y-%m-%d %H:%M') backup ok"
