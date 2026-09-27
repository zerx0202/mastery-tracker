"""Cisza na czas kopii bazy (27.09). backup.sh kopiuje baze API backup
SQLite krokami, a kazdy zapis innego polaczenia RESTARTUJE kopie od zera:
na Macu co drugi backup konczyl sie limitem 600 s, a przez ten czas kopia
mielila virtiofs i blokowala zapisy po grze ("database is locked", 500 na
/lobby i /live). Kopia jednym krokiem trwa 17,9 s (sonda 27.09) - dluzej
niz busy_timeout 10 s, wiec zostaje kopia krokami przy CICHEJ bazie:
backup.sh zglasza poczatek i koniec, a w tym oknie serwer oddaje agentowi
pusta prace w tle (snowball, brakujace gry, timeline'y). Agent przechodzi
w bezczynnosc sam, bez zmian po jego stronie. Bezpiecznik: okno wygasa."""
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from tests.conftest import insert_row


def _seed_work():
    with db.connect() as con:
        insert_row(con, "snowball_seen", puuid="p" * 36, added_at=1, checked_at=0)
    db.log_event("grade", {"game_id": 77}, 1)


def test_background_work_pauses_during_backup(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "missing_own_games", lambda limit: [77])
    monkeypatch.setattr(db, "missing_timelines", lambda limit: [77])
    _seed_work()
    c = TestClient(app_main.app)
    assert c.get("/api/snowball/next").json()["puuids"]
    assert c.post("/api/backup/begin").status_code == 200
    assert c.get("/api/snowball/next").json() == {"puuids": [], "paused": "backup"}
    assert c.get("/api/history/missing").json() == {"game_ids": [], "paused": "backup"}
    assert c.get("/api/timelines/missing").json() == {"game_ids": [], "paused": "backup"}
    assert c.post("/api/backup/end").status_code == 200
    assert c.get("/api/snowball/next").json()["puuids"]
    assert c.get("/api/history/missing").json()["game_ids"] == [77]


def test_quiet_window_expires_on_its_own(fresh_db, monkeypatch):
    c = TestClient(app_main.app)
    c.post("/api/backup/begin")
    assert app_main.backup_quiet()
    later = time.time() + app_main.BACKUP_QUIET_MAX + 1
    monkeypatch.setattr(app_main.time, "time", lambda: later)
    assert not app_main.backup_quiet(), "zabity backup.sh nie moze uciszyc agenta na zawsze"


def test_backup_report_also_ends_quiet_window(fresh_db):
    c = TestClient(app_main.app)
    c.post("/api/backup/begin")
    c.post("/api/backup/report", json={"ok": True, "note": "ok"})
    assert not app_main.backup_quiet()


def test_quiet_window_needs_token(fresh_db, monkeypatch):
    monkeypatch.setattr(app_main, "API_TOKEN", "sekret")
    c = TestClient(app_main.app)
    assert c.post("/api/backup/begin").status_code == 401
    assert c.post("/api/backup/end").status_code == 401


def test_backup_script_brackets_the_copy_with_the_quiet_window():
    sh = (Path(__file__).resolve().parents[1] / "backup.sh").read_text(encoding="utf-8")
    assert '"$API/api/backup/$1"' in sh
    nl = chr(10)
    begin = sh.index(nl + "quiet begin")
    copy = sh.index("s.backup(d")
    end = sh.index(nl + "quiet_end", copy)
    assert begin < copy < end
    assert re.search(r"trap quiet_end EXIT", sh), "koniec ciszy tez przy bledzie"
