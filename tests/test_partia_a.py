"""Partia A z przegladu 2.09 (wieczor): potok przestaje klamac o wyniku zapisu.

Backend: /grade i /eog oddaja 5xx, gdy zapis realnie padl (200 rozbrajalo
kolejke dyskowa agenta); czujki w System (eog bez oceny, zdrowie klucza
Riot, wiek balansu). Strona agenta: tests/test_agent.py."""
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row


# ---------- A1: uczciwe statusy na /grade i /eog ----------

def test_grade_500_when_nothing_saved(fresh_db, monkeypatch):
    """Awaria bazy przy zapisie oceny NIE moze konczyc sie 200 - agent
    kasuje wtedy wpis z kolejki i jedyna bezpowrotna dana znika."""
    def boom(*a, **k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(db, "save_grade", boom)
    monkeypatch.setattr(db, "save_grade_raw", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/grade",
                    json={"updates": [{"gameId": 7, "grade": "S-"}]})
    assert r.status_code == 500
    assert "disk I/O error" in r.text


def test_grade_200_when_only_archive_fails(fresh_db, monkeypatch):
    """Awaria samego archiwum grade_raw nie blokuje zapisu oceny -
    ta intencja z komentarza zostaje, 5xx jest tylko przy zerze zapisow."""
    def boom(*a, **k):
        raise RuntimeError("archiwum lezy")
    monkeypatch.setattr(db, "save_grade_raw", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/grade",
                    json={"updates": [{"gameId": 7, "grade": "S-",
                                       "championId": 45}]})
    assert r.status_code == 200
    body = r.json()
    assert body["new"] == 1 and body["errors"]


def test_grade_200_on_duplicate_resend(fresh_db):
    """Dosylka z kolejki po naprawionym bledzie: duplikat (new=0, zero
    bledow) to sukces, nie 5xx - inaczej kolejka mielilaby wpis wiecznie."""
    client = TestClient(app, raise_server_exceptions=False)
    payload = {"updates": [{"gameId": 7, "grade": "S-", "championId": 45}]}
    assert client.post("/api/grade", json=payload).status_code == 200
    r = client.post("/api/grade", json=payload)
    assert r.status_code == 200 and r.json()["new"] == 0


def test_eog_500_on_db_error(fresh_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "save_eog", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/eog", json={"block": {"gameId": 9, "teams": []}})
    assert r.status_code == 500
    assert "database is locked" in r.text


def test_eog_still_200_on_success(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    block = {"gameId": 9, "teams": [{"teamId": 100, "players": [
        {"isLocalPlayer": True, "championId": 45,
         "puuid": "a" * 36, "stats": {"WIN": 1}}]}]}
    r = client.post("/api/eog", json={"block": block})
    assert r.status_code == 200 and r.json()["stored"] is True


# ---------- A6: czujki ----------

def test_pipeline_sanity_counts_eog_without_grade(fresh_db):
    now = int(time.time())
    with db.connect() as con:
        # eog z ocena - zdrowy; eog bez oceny - kanal ocen przecieka
        insert_row(con, "eog_raw", match_id="EUW1_1", game_id=1,
                   payload=b"x", captured_at=now)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="A-", observed_at=now)
        insert_row(con, "eog_raw", match_id="EUW1_2", game_id=2,
                   payload=b"x", captured_at=now)
    assert db.pipeline_sanity()["eog_bez_oceny"] == 1


def test_riot_auth_state_change_logged_once(fresh_db):
    from app import main
    main.note_riot_auth(False, 403)
    main.note_riot_auth(False, 403)   # ten sam stan - bez drugiego eventu
    main.note_riot_auth(True, 200)
    st = db.get_json_setting("riot_api_auth")
    assert st["ok"] is True and st["status"] == 200
    changes = [e for e in db.recent_events(20) if e["kind"] == "riot_auth"]
    assert len(changes) == 2          # dead -> ok, po jednym na zmiane


def test_health_exposes_sentries(fresh_db):
    from app import main
    main.note_riot_auth(False, 401)
    db.set_json_setting("mayhem_balance", {"fetched_at": 1700000000,
                                           "champions": {}})
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["riot_auth"]["ok"] is False
    assert h["balance_fetched_at"] == 1700000000
    assert "eog_bez_oceny" in h["pipeline"]
