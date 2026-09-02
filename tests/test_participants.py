"""Karta 9: tozsamosci graczy per mecz. flatten_eog_stats odrzuca pola
nienumeryczne, wiec puuid ginal przy splaszczaniu bloku eog - lacze
match_id -> puuid zyje tylko w blobach eog_raw. match_participant utrwala
je pod ta sama numeracja slotow co player_stat, zeby JOIN byl trywialny."""
from fastapi.testclient import TestClient

from app import db
from app.main import app


def _block():
    return {
        "gameId": 77,
        "teams": [
            {"teamId": 100, "players": [
                {"championId": 45, "puuid": "p-jeden", "isLocalPlayer": True,
                 "stats": {"kills": 5}, "items": []},
                {"championId": 12, "puuid": "p-dwa",
                 "stats": {"kills": 1}, "items": []},
            ]},
            {"teamId": 200, "players": [
                {"championId": 99, "puuid": "p-trzy",
                 "stats": {"kills": 2}, "items": []},
            ]},
        ],
    }


def test_participants_share_numbering_with_player_stat(fresh_db):
    block = _block()
    db.flatten_eog_stats(block, "EUW1_77")
    assert db.save_match_participants(block, "EUW1_77") == 3

    with db.connect() as con:
        parts = {r["participant_no"]: dict(r) for r in con.execute(
            "SELECT * FROM match_participant WHERE match_id='EUW1_77'")}
        champs = {r["participant_no"]: r["champion_id"] for r in con.execute(
            "SELECT DISTINCT participant_no, champion_id FROM player_stat "
            "WHERE match_id='EUW1_77'")}

    assert parts[1]["puuid"] == "p-jeden" and parts[1]["team_id"] == 100
    assert parts[2]["puuid"] == "p-dwa" and parts[2]["team_id"] == 100
    assert parts[3]["puuid"] == "p-trzy" and parts[3]["team_id"] == 200
    # ten sam slot wskazuje tego samego gracza w obu tabelach
    assert champs == {1: 45, 2: 12, 3: 99}


def test_missing_puuid_keeps_slot_numbering(fresh_db):
    block = _block()
    del block["teams"][0]["players"][1]["puuid"]
    assert db.save_match_participants(block, "EUW1_77") == 2
    with db.connect() as con:
        slots = [r["participant_no"] for r in con.execute(
            "SELECT participant_no FROM match_participant "
            "WHERE match_id='EUW1_77' ORDER BY participant_no")]
    # slot 2 wypada, ale 3 nie przesuwa sie na 2
    assert slots == [1, 3]


def test_save_is_idempotent(fresh_db):
    block = _block()
    db.save_match_participants(block, "EUW1_77")
    db.save_match_participants(block, "EUW1_77")
    with db.connect() as con:
        n = con.execute("SELECT COUNT(*) c FROM match_participant").fetchone()["c"]
    assert n == 3


def test_backfill_from_eog_blobs(fresh_db):
    db.save_eog(_block(), "euw1", 1700000000)
    with db.connect() as con:
        assert con.execute(
            "SELECT COUNT(*) c FROM match_participant").fetchone()["c"] == 0

    out = db.backfill_participants_from_eog()
    assert out == {"blobs": 1, "filled": 1, "empty": 0, "rows": 3}

    # drugi przebieg niczego nie dubluje
    out2 = db.backfill_participants_from_eog()
    assert out2["rows"] == 3
    with db.connect() as con:
        n = con.execute("SELECT COUNT(*) c FROM match_participant").fetchone()["c"]
    assert n == 3


def test_eog_endpoint_stores_participants(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/eog", json={"block": _block()})
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True
    assert body["participants"] == 3
    with db.connect() as con:
        n = con.execute(
            "SELECT COUNT(*) c FROM match_participant WHERE match_id='EUW1_77'"
        ).fetchone()["c"]
    assert n == 3
