#!/bin/zsh
# Backup bazy mastery-tracker na desktopa przez Tailscale (SFTP).
# Backupujemy WYLACZNIE plik bazy - .env z kluczem API nigdy nie wyjezdza.
set -euo pipefail

export RESTIC_REPOSITORY="sftp:desktop:/C:/backup/mastery"
export RESTIC_PASSWORD_FILE="$HOME/.restic-pass"
export PATH="/opt/homebrew/bin:$PATH"

SNAP="/tmp/mastery-snap.db"

cd "$HOME/stacks/riot"

# Jawna bramka na kontener PRZED jakakolwiek proba kopii: brak backupu ma
# krzyczec niezerowym kodem i komunikatem, a nie przechodzic po cichu.
CID="$(/opt/homebrew/bin/docker compose ps -q backend 2>/dev/null || true)"
if [ -z "$CID" ] || [ "$(/opt/homebrew/bin/docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null)" != "true" ]; then
  echo "BLAD: kontener backend nie dziala - backupu NIE zrobiono" >&2
  exit 1
fi

# Kopie robi kontener, a nie host: baza jest na bind moncie przez virtiofs
# i dwa procesy z roznych stron montu potrafia sie o nia pobic.
# Zrodlo otwierane read-only przez URI: gdyby pliku bazy nie bylo, connect
# nie ma prawa stworzyc pustego i "zbackupowac" go jako sukces.
/opt/homebrew/bin/docker compose exec -T backend \
  python -c "import sqlite3; s=sqlite3.connect('file:/code/data/mastery.db?mode=ro', uri=True); d=sqlite3.connect('/code/data/_snap.db'); s.backup(d); d.close(); s.close()"
mv "$HOME/stacks/riot/data/_snap.db" "$SNAP"
sqlite3 "$SNAP" "PRAGMA integrity_check;" | grep -q '^ok$' || { echo "BAZA USZKODZONA - przerywam"; exit 1; }

restic backup "$SNAP" --tag mastery

rm -f "$SNAP"

# raz na tydzien: retencja + weryfikacja pod wspolnym stampem. prune
# przepisuje repozytorium (najdrozsza operacja restica), wiec po kazdej
# grze robimy sam snapshot, a porzadki hurtowo
STAMP="$HOME/.restic-last-check"
if [ ! -f "$STAMP" ] || [ $(( $(date +%s) - $(stat -f %m "$STAMP") )) -gt 604800 ]; then
  restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
  restic check --read-data-subset=10% && touch "$STAMP"
fi

echo "$(date '+%Y-%m-%d %H:%M') backup ok"
