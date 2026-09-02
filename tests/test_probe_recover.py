"""Wejście w agenta nr 2, strona backendu: konsola LCU (karta 42)
i odzysk gier po ID (P6)."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row


def test_probe_full_flow(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/probe", json={"path": "/lol-summoner/v1/current-summoner"})
    assert r.status_code == 200
    pid = r.json()["id"]

    pend = client.get("/api/probe/pending").json()["probes"]
    assert [p["id"] for p in pend] == [pid]

    r = client.post("/api/probe/result",
                    json={"id": pid, "http_status": 200,
                          "response": '{"puuid": "x"}'})
    assert r.status_code == 200
    assert client.get("/api/probe/pending").json()["probes"] == []

    out = client.get(f"/api/probe/{pid}").json()
    assert out["http_status"] == 200 and out["answered_at"]
    assert out["response"] == '{"puuid": "x"}' and out["truncated"] == 0


def test_probe_rejects_bad_paths(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    for bad in ("lol-summoner", "/a/../b", "", "/" + "x" * 400):
        assert client.post("/api/probe", json={"path": bad}).status_code == 400


def test_probe_truncates_and_prunes(fresh_db):
    db.probe_answer(db.probe_create("/x", 1), 200,
                    "a" * (db.PROBE_MAX_RESPONSE + 5), 2)
    with db.connect() as con:
        row = con.execute("SELECT * FROM lcu_probe").fetchone()
    assert row["truncated"] == 1
    assert len(row["response"]) == db.PROBE_MAX_RESPONSE

    for i in range(db.PROBE_KEEP + 7):
        db.probe_create(f"/p{i}", i)
    with db.connect() as con:
        n = con.execute("SELECT COUNT(*) c FROM lcu_probe").fetchone()["c"]
    assert n == db.PROBE_KEEP


def test_missing_own_games_from_all_sources(fresh_db):
    with db.connect() as con:
        # znane z trzech zrodel, bez statystyk
        insert_row(con, "eog_raw", match_id="EUW1_11", game_id=11,
                   payload=b"x", captured_at=1)
        insert_row(con, "grade_observation", match_id="EUW1_22", game_id=22,
                   champion_id=45, grade="A-", observed_at=1)
        insert_row(con, "champ_select_pool", ts=1, champion_ids="[1]",
                   pool_size=1, match_id="EUW1_33")
        # znana i ZAPISANA - nie jest brakiem
        insert_row(con, "eog_raw", match_id="EUW1_44", game_id=44,
                   payload=b"x", captured_at=1)
        insert_row(con, "match_player", match_id="EUW1_44", champion_id=45,
                   duration=1200)
    assert db.missing_own_games() == [33, 22, 11]
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/history/missing?limit=2").json()["game_ids"] == [33, 22]
    assert db.pipeline_sanity()["missing_games"] == 3
