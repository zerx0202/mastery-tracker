"""Partia Q (4.09 wieczor, po grze z botami i zrzutach czlowieka):
1. Bot bez tagu ("Jade_Nasus bot", 5 gier przeciw) przeszedl przez filtry
   po tagu BOT (O/P) - cecha bota to nazwa konczaca sie na " bot".
2. Catch-all WS: pelna gra dala 2676 zdarzen, oceny 0 (ocena z pollingu)
   - licznik uri z rodziny end-of-game/mastery ma pokazac, co LCU publikuje.
Front (smoke w test_ui_smoke): sojusznicy jako zetony z ikona i historia,
patch bez "uzasadnienia Riota"."""
import asyncio

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row

MY, A, BOT = "m" * 36, "a" * 36, "b" * 36


def test_bot_name_is_the_feature_not_the_tag():
    assert db._bot_name("Jade_Nasus bot") and db._bot_name("Annie Bot#BOT")
    assert not db._bot_name("Robot#EUW") and not db._bot_name("Zed#BOT") and not db._bot_name("")
    assert db._bot_identity({"gameName": "", "summonerName": "Jade_Nasus bot", "tagLine": ""})
    assert not db._bot_identity({"gameName": "Zed", "tagLine": "BOT"})


def test_eog_bot_without_flag_and_tag_is_skipped(fresh_db):
    block = {"gameId": 1, "teams": [
        {"teamId": 100, "players": [
            {"puuid": MY, "riotIdGameName": "Ja", "riotIdTagLine": "1", "championId": 45,
             "isLocalPlayer": True, "stats": {"kills": 1}}]},
        {"teamId": 200, "players": [
            {"puuid": BOT, "summonerName": "Jade_Nasus bot", "championId": 75,
             "stats": {"kills": 0}},                       # bez botPlayer, bez Riot ID
            {"puuid": A, "riotIdGameName": "Zed", "riotIdTagLine": "EUW", "championId": 238,
             "stats": {"kills": 2}}]}]}
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45, duration=1200,
                   game_mode="JADE", win=1, game_creation=1_700_000_000_000)
    assert db.save_match_participants(block, "EUW1_1") == 2
    with db.connect() as con:
        assert {r["puuid"] for r in con.execute("SELECT puuid FROM match_participant")} == {MY, A}
        assert BOT not in {r["puuid"] for r in con.execute("SELECT puuid FROM player_name")}


def test_lcu_bot_without_tag_is_skipped(fresh_db):
    g = {"gameId": 9, "platformId": "EUW1", "gameMode": "JADE", "queueId": 4320,
         "participantIdentities": [
             {"participantId": 1, "player": {"puuid": MY, "gameName": "Ja", "tagLine": "1"}},
             {"participantId": 2, "player": {"puuid": BOT, "gameName": "",
                                             "summonerName": "Jade_Nasus bot", "tagLine": ""}}],
         "participants": [
             {"participantId": 1, "teamId": 100, "championId": 45, "stats": {"kills": 1}},
             {"participantId": 2, "teamId": 200, "championId": 75, "stats": {"kills": 0}}]}
    db.save_lcu_participants(g, "EUW1_9", MY)
    with db.connect() as con:
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM match_participant")] == [MY]
        assert [r["name"] for r in con.execute("SELECT name FROM player_name")] == ["Ja#1"]


def test_startup_cleanup_drops_tagless_bots_saved_before(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_participant", match_id="EUW1_5", participant_no=7,
                   puuid=BOT, team_id=200)
        insert_row(con, "player_name", puuid=BOT, name="Jade_Nasus bot", seen_at=1)
        insert_row(con, "player_name", puuid=A, name="Robot#EUW", seen_at=1)
    db.migrate()                                          # upgrade_drop_bots
    with db.connect() as con:
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM player_name")] == [A]
        assert con.execute("SELECT COUNT(*) c FROM match_participant").fetchone()["c"] == 0
    assert db.recurring_players(MY, min_games=1) == []


def test_ws_uri_family_counter_reaches_health(fresh_db):
    from tests.test_partia_k import GameflowLcu, _agent

    async def run():
        a = _agent(GameflowLcu())
        await a.dispatch_ws("/lol-end-of-game/v1/eog-stats-block", {"x": 1})
        await a.dispatch_ws("/lol-end-of-game/v1/eog-stats-block", {"x": 2})
        await a.dispatch_ws("/lol-champion-mastery/v1/notifications", {})
        await a.dispatch_ws("/lol-chat/v1/conversations", {})
        assert a.ws_events["uris"] == {"/lol-end-of-game/v1/eog-stats-block": 2,
                                       "/lol-champion-mastery/v1/notifications": 1}
        assert a.ws_events["total"] == 4 and a.ws_events["mastery"] == 0
        return dict(a.ws_events)
    ev = asyncio.run(run())
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/agent/health", json={"queue": 0, "bad": 0, "ws_ok": True, "ws_events": ev})
    h = client.get("/api/system/health").json()
    assert h["agent_health"]["ws_events"]["uris"]["/lol-end-of-game/v1/eog-stats-block"] == 2
