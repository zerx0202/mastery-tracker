"""Filtr per queueId (pomysl E, dowody: C5 + audyt eksportu 2.09).

Kolejka 3270 to custom game "ARAM: Mayhem" (category Custom, queueRewards
wylaczone - zero ocen i punktow maestrii), a zglasza gameMode=KIWI - dwie
gry treningowe weszly do norm jako pelnoprawne dane misji. Zamiast
odrzucac (surowiec sie nie wyrzuca), zapis nadaje im odrozniamy tryb
KIWI_CUSTOM - wszystkie filtry game_mode="KIWI" odsiewaja je same."""
from fastapi.testclient import TestClient

from app import db, model
from app.main import app
from tests.conftest import insert_row


def test_effective_mode_mapping():
    assert db.effective_mode("KIWI", 2400) == "KIWI"
    assert db.effective_mode("KIWI", 3270) == "KIWI_CUSTOM"
    # brak queueId w danych to nie dowod customa - nie karac braku pola
    assert db.effective_mode("KIWI", None) == "KIWI"
    # tryby bez przypisanej kolejki matchmakingu przechodza bez zmian
    assert db.effective_mode("CLASSIC", 420) == "CLASSIC"
    assert db.effective_mode("JADE", 4320) == "JADE"
    assert db.effective_mode(None, 3270) is None


def _lcu_game(gid, qid, mode="KIWI"):
    return {"gameId": gid, "platformId": "EUW1", "queueId": qid,
            "gameMode": mode, "gameCreation": 1700000000000,
            "gameDuration": 1200,
            "participants": [{"participantId": 1, "championId": 45,
                              "stats": {"win": True, "kills": 1},
                              "timeline": {}}],
            "participantIdentities": [{"participantId": 1,
                                       "player": {"puuid": "a" * 36}}]}


def test_save_lcu_game_tags_custom_queue(fresh_db):
    db.save_lcu_game(_lcu_game(1, 2400))
    db.save_lcu_game(_lcu_game(2, 3270))
    with db.connect() as con:
        modes = {r["match_id"]: (r["game_mode"], r["queue_id"])
                 for r in con.execute(
                     "SELECT match_id, game_mode, queue_id FROM match_player")}
    assert modes["EUW1_1"] == ("KIWI", 2400)
    assert modes["EUW1_2"] == ("KIWI_CUSTOM", 3270)   # surowe queue_id zostaje


def test_custom_games_invisible_to_mission_filters(fresh_db):
    db.save_lcu_game(_lcu_game(1, 2400))
    db.save_lcu_game(_lcu_game(2, 3270))
    with db.connect() as con:
        for gid in (1, 2):
            insert_row(con, "grade_observation", match_id=f"EUW1_{gid}",
                       game_id=gid, champion_id=45, grade="S", observed_at=gid)
    rows = model.training_rows("KIWI")
    assert len(rows) == 1                      # custom nie karmi modelu
    assert model.own_games_map("KIWI") == {45: 1}


def test_upgrade_custom_modes_backfills_and_is_idempotent(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_10", champion_id=14,
                   duration=489, game_mode="KIWI", queue_id=3270)
        insert_row(con, "match_player", match_id="EUW1_11", champion_id=14,
                   duration=1200, game_mode="KIWI", queue_id=2400)
    db.upgrade_custom_modes()
    db.upgrade_custom_modes()                  # idempotencja jak w migrate()
    with db.connect() as con:
        modes = {r["match_id"]: r["game_mode"] for r in con.execute(
            "SELECT match_id, game_mode FROM match_player")}
    assert modes == {"EUW1_10": "KIWI_CUSTOM", "EUW1_11": "KIWI"}


def test_snowball_ingest_rejects_custom_queue(fresh_db):
    g = {"gameId": 77, "gameMode": "KIWI", "queueId": 3270,
         "gameDuration": 1200, "gameCreation": 1700000000000,
         "participants": [{"championId": 45, "teamId": 100,
                           "stats": {"kills": 3}}]}
    kiwi, rows = db.snowball_ingest("b" * 36, [g])
    assert (kiwi, rows) == (0, 0), "custom obcego gracza nie karmi norm"
    g["queueId"] = 2400
    kiwi, rows = db.snowball_ingest("b" * 36, [g])
    assert kiwi == 1 and rows > 0


def test_health_counts_custom_games(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_10", champion_id=14,
                   duration=489, game_mode="KIWI_CUSTOM", queue_id=3270)
        insert_row(con, "match_player", match_id="EUW1_11", champion_id=14,
                   duration=1200, game_mode="KIWI", queue_id=2400)
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["custom_games"] == 1
