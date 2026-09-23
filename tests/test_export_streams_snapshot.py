"""Eksport bazy przy 5,7 mln wierszy statystyk (23.09: curl zapisal 0 B).
/api/export skladal cala baze w pamieci jako slowniki i jeden JSON - przy
tej skali proces serwera nie mial szans. Teraz oba kanaly wyjscia czytaja
SPOJNA kopie robiona krokami (jak backup.sh, bez dlugiej blokady zywej
bazy), a JSON idzie strumieniem porcjami wierszy. /api/export/db oddaje
sama kopie SQLite - razem z blobami, gotowa pod DB_PATH narzedzi."""
import json
import sqlite3

from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from tests.conftest import insert_row


def _seed(n=2500):
    with db.connect() as con:
        for i in range(n):
            insert_row(con, "player_stat", match_id=f"SB_{i}", participant_no=1,
                       champion_id=45, stat_key="kills", stat_value=i)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="S-", observed_at=1)


def _leftovers():
    return sorted(p.name for p in db.DB_PATH.parent.glob("_export-*"))


def test_json_export_streams_every_row(fresh_db):
    _seed()
    client = TestClient(app_main.app)
    r = client.get("/api/export")
    assert r.status_code == 200
    data = json.loads(r.content)
    assert len(data["player_stat"]) == 2500
    assert data["grade_observation"][0]["grade"] == "S-"
    # bloby (kolumny payload) poza eksportem JSON - jak dotad
    assert all("payload" not in k for rows in data.values() for row in rows[:1] for k in row)
    assert _leftovers() == [], "kopia robocza eksportu ma zniknac"


def test_json_export_is_sent_in_chunks(fresh_db, monkeypatch):
    # strumien, nie jeden wielki bufor: porcje wierszy
    _seed()
    # (klient testowy buforuje cala odpowiedz, wiec patrzymy na generator)
    monkeypatch.setattr(app_main, "EXPORT_CHUNK_ROWS", 500)
    path = app_main._export_snapshot()
    chunks = list(app_main._export_json_chunks(path))
    stat_chunks = [c for c in chunks if '"stat_key"' in c]
    assert len(stat_chunks) == 5
    assert max(c.count('"stat_key"') for c in stat_chunks) <= 500
    assert len(json.loads("".join(chunks))["player_stat"]) == 2500
    assert not path.exists(), "kopia robocza znika po ostatniej porcji"


def test_db_export_is_a_complete_sqlite_copy(fresh_db, tmp_path):
    _seed(10)
    client = TestClient(app_main.app)
    r = client.get("/api/export/db")
    assert r.status_code == 200
    assert r.content[:16] == b"SQLite format 3\x00"
    out = tmp_path / "kopia.db"
    out.write_bytes(r.content)
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT COUNT(*) FROM player_stat").fetchone()[0] == 10
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert _leftovers() == []


def test_exports_require_token(fresh_db, monkeypatch):
    monkeypatch.setattr(app_main, "API_TOKEN", "sekret")
    client = TestClient(app_main.app)
    assert client.get("/api/export").status_code == 401
    assert client.get("/api/export/db").status_code == 401
    assert client.get("/api/export/db", headers={"x-api-token": "sekret"}).status_code == 200
