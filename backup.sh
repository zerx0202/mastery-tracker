#!/bin/zsh
# Backup bazy mastery-tracker na desktopa przez Tailscale (SFTP).
# Backupujemy WYLACZNIE plik bazy - .env z kluczem API nigdy nie wyjezdza.
set -euo pipefail

export RESTIC_REPOSITORY="sftp:desktop:/C:/backup/mastery"
export RESTIC_PASSWORD_FILE="$HOME/.restic-pass"
export PATH="/opt/homebrew/bin:$PATH"

SNAP="/tmp/mastery-snap.db"
API="${MASTERY_API:-http://127.0.0.1:8000}"

cd "$HOME/stacks/riot"

# Jawna bramka na kontener PRZED jakakolwiek proba kopii: brak backupu ma
# krzyczec niezerowym kodem i komunikatem, a nie przechodzic po cichu.
CID="$(/opt/homebrew/bin/docker compose ps -q backend 2>/dev/null || true)"
if [ -z "$CID" ] || [ "$(/opt/homebrew/bin/docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null)" != "true" ]; then
  echo "BLAD: kontener backend nie dziala - backupu NIE zrobiono" >&2
  exit 1
fi

# (27.09) Cisza na czas kopii: kazdy zapis innego polaczenia restartuje kopie
# API backup od zera, a snowball agenta pisze co 10 s - kopia krecila sie do
# limitu 600 s i blokowala zapisy po grze. begin = serwer oddaje agentowi
# pusta prace w tle; end = wraca normalny ruch. end rowniez przy bledzie
# (trap), a serwer i tak wygasza cisze sam po 15 min. Brak API nie blokuje
# backupu (|| true) - najwyzej kopia potrwa dluzej, jak dotad.
quiet() {
  if [ -n "${MASTERY_API_TOKEN:-}" ]; then
    curl -s -m 10 -o /dev/null -X POST -H "X-API-Token: $MASTERY_API_TOKEN" \
      "$API/api/backup/$1" || true
  else
    curl -s -m 10 -o /dev/null -X POST "$API/api/backup/$1" || true
  fi
}
# koniec ciszy dokladnie raz: po kopii albo z pulapki przy bledzie
QUIET_ON=0
quiet_end() {
  if [ "$QUIET_ON" = 1 ]; then
    QUIET_ON=0
    quiet end
  fi
  return 0
}
trap quiet_end EXIT
quiet begin
QUIET_ON=1
# paczka snowballa w locie (agent co 10 s) konczy sie, zanim ruszy kopia
sleep 12

# Kopie robi kontener, a nie host: baza jest na bind moncie przez virtiofs
# i dwa procesy z roznych stron montu potrafia sie o nia pobic.
# Zrodlo otwierane read-only przez URI: gdyby pliku bazy nie bylo, connect
# nie ma prawa stworzyc pustego i "zbackupowac" go jako sukces.
# Kopia w krokach po 4096 stron (16 MB): blokada SHARED zrodla trwa tylko
# w trakcie kroku, miedzy krokami zapisujacy (eog, timeline, snowball)
# dostaja EXCLUSIVE, a czytelnicy dashboardu nie czekaja na SHARED trzymany
# przez cala kopie (log 10.09: 500 "locked"). `sleep` to odczekanie po
# BUSY/LOCKED przed ponowieniem kroku, nie przerwa miedzy krokami. Zapis
# w trakcie kroku = SQLite zaczyna kopie od nowa, wiec kopia jest spojna.
/opt/homebrew/bin/docker compose exec -T backend \
  python -c "import sqlite3; s=sqlite3.connect('file:/code/data/mastery.db?mode=ro', uri=True); d=sqlite3.connect('/code/data/_snap.db'); s.backup(d, pages=4096, sleep=0.02); d.close(); s.close()"
mv "$HOME/stacks/riot/data/_snap.db" "$SNAP"
quiet_end
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
