"""Fala 1: trade_ids, mediana finalnych pul, popularnosc SB, gry na patchu."""
from app import db
from tests.conftest import insert_row


def test_trade_ids_roundtrip(fresh_db):
    db.set_lobby([1, 2, 3], "KIWI", "limited", 100, trade_ids=[3, 2])
    lob = db.get_lobby()
    assert lob["trade_ids"] == [2, 3]

    pid = db.save_pool([1, 2, 3], "KIWI", 2400, "limited", 100, trade_ids=[3])
    with db.connect() as con:
        row = con.execute("SELECT trade_ids FROM champ_select_pool WHERE id=?",
                          (pid,)).fetchone()
    assert row["trade_ids"] == "[3]"


def test_pool_rotation_updates_trade_ids(fresh_db):
    """Rotacja z lawka: ta sama unia -> ten sam wiersz, ale trade_ids dogania
    stan. Po zlinkowaniu z meczem ta sama unia to juz nowa pula."""
    pid = db.save_pool([1, 2, 3], "KIWI", 2400, "limited", 100, trade_ids=[3])
    assert db.save_pool([1, 2, 3], "KIWI", 2400, "limited", 105, trade_ids=[2]) == pid
    with db.connect() as con:
        row = con.execute("SELECT trade_ids, ts FROM champ_select_pool WHERE id=?",
                          (pid,)).fetchone()
    assert row["trade_ids"] == "[2]"
    assert row["ts"] == 100                       # ts pierwszego stanu zostaje
    db.link_pool_to_match("EUW1_1", 2, 0, 200)
    assert db.save_pool([1, 2, 3], "KIWI", 2400, "limited", 300, trade_ids=[2]) != pid


def test_push_lobby_logs_event_once_per_pool(fresh_db):
    """Rotacje trafiaja na ten sam pool_id -> jeden event champ_select na pule,
    nie na kazdy POST; nowa unia = nowa pula = nowy event."""
    from fastapi.testclient import TestClient
    from app.main import app, state
    state.pop("last_pool_id", None)
    client = TestClient(app)
    body = {"champion_ids": [1, 2, 3], "trade_ids": [3], "queue": "KIWI",
            "pool_kind": "limited", "queue_id": 2400}
    first = client.post("/api/lobby", json=body).json()["pool_id"]
    assert client.post("/api/lobby", json={**body, "trade_ids": [2]}).json()["pool_id"] == first
    assert len(db.recent_events(kind="champ_select")) == 1
    assert db.get_lobby()["trade_ids"] == [2]          # UI dostaje swiezy stan
    client.post("/api/lobby", json={**body, "champion_ids": [1, 2, 3, 4]})
    assert len(db.recent_events(kind="champ_select")) == 2


def test_median_final_pool_size(fresh_db):
    assert db.median_final_pool_size() == 11          # pusta baza -> stala
    db.save_pool(list(range(1, 4)), "KIWI", 2400, "limited", 10)   # stan czesciowy
    db.save_pool(list(range(1, 12)), "KIWI", 2400, "limited", 20)
    db.link_pool_to_match("EUW1_1", 5, 0, 30)
    db.save_pool(list(range(1, 14)), "KIWI", 2400, "limited", 40)
    db.link_pool_to_match("EUW1_2", 5, 0, 50)
    db.save_pool(list(range(1, 171)), "CLASSIC", 420, "full", 60)  # pula "full"
    assert db.median_final_pool_size() == 12          # mediana z {11, 13}


def test_sb_popularity_and_patch_games(fresh_db):
    with db.connect() as con:
        for mid, cid in (("SB_1", 7), ("SB_2", 7), ("SB_3", 45)):
            insert_row(con, "player_stat", match_id=mid, participant_no=1,
                       champion_id=cid, team_id=100, is_local=0,
                       stat_key="gold", stat_value=1.0)
        insert_row(con, "match_player", match_id="EUW1_9", duration=1200,
                   game_mode="KIWI", champion_id=1, patch="16.17")
        insert_row(con, "match_player", match_id="EUW1_10", duration=1200,
                   game_mode="KIWI", champion_id=1, patch="16.17")
        insert_row(con, "match_player", match_id="EUW1_11", duration=1200,
                   game_mode="ARAM", champion_id=1, patch="16.17")
        insert_row(con, "match_player", match_id="EUW1_12", duration=1200,
                   game_mode="KIWI", champion_id=1, patch="16.16")
        con.commit()
    assert db.champion_sb_popularity() == {7: 2, 45: 1}
    assert db.games_on_patch("16.17") == 3
    assert db.games_on_patch("16.17", "KIWI") == 2
