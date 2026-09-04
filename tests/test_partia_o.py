"""Partia O (4.09): boty poza karta 9. Blok eog daje botom puuid i nazwe
("Jade_Taric bot#BOT"), a filtr odrzucal tylko graczy BEZ puuid - bot
wyladowal w rejestrze powtarzajacych sie graczy z 5 grami przeciw. Slot
w numeracji zostaje (player_stat liczy botow tak samo)."""
from app import db
from tests.conftest import insert_row

MY, A, BOT = "m" * 36, "a" * 36, "b" * 36


def _block():
    return {"gameId": 1, "teams": [
        {"teamId": 100, "players": [
            {"puuid": MY, "riotIdGameName": "Ja", "riotIdTagLine": "1", "championId": 45,
             "isLocalPlayer": True, "botPlayer": False, "stats": {"kills": 1}}]},
        {"teamId": 200, "players": [
            {"puuid": BOT, "riotIdGameName": "Jade_Taric bot", "riotIdTagLine": "BOT",
             "championId": 44, "botPlayer": True, "stats": {"kills": 0}},
            {"puuid": A, "riotIdGameName": "Zed", "riotIdTagLine": "EUW", "championId": 238,
             "botPlayer": False, "stats": {"kills": 2}}]}]}


def test_eog_bots_get_no_identity_but_keep_their_slot(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45, duration=1200,
                   game_mode="JADE", win=1, game_creation=1_700_000_000_000)
    assert db.save_match_participants(_block(), "EUW1_1") == 2
    db.flatten_eog_stats(_block(), "EUW1_1")
    with db.connect() as con:
        parts = {r["puuid"]: r["participant_no"] for r in con.execute(
            "SELECT puuid, participant_no FROM match_participant")}
        names = {r["puuid"] for r in con.execute("SELECT puuid FROM player_name")}
    assert parts == {MY: 1, A: 3} and BOT not in names     # slot 2 nalezy do bota
    assert db.recurring_players(MY, min_games=1) and all(
        p["puuid"] != BOT for p in db.recurring_players(MY, min_games=1))


def test_lcu_full_game_skips_bot_identities(fresh_db):
    g = {"gameId": 9, "platformId": "EUW1", "gameMode": "JADE", "queueId": 4320,
         "participantIdentities": [
             {"participantId": 1, "player": {"puuid": MY, "gameName": "Ja", "tagLine": "1"}},
             {"participantId": 2, "player": {"puuid": BOT, "gameName": "Jade_Taric bot",
                                             "tagLine": "BOT"}}],
         "participants": [
             {"participantId": 1, "teamId": 100, "championId": 45, "stats": {"kills": 1}},
             {"participantId": 2, "teamId": 200, "championId": 44, "stats": {"kills": 0}}]}
    db.save_lcu_participants(g, "EUW1_9", MY)
    with db.connect() as con:
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM match_participant")] == [MY]
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM player_name")] == [MY]


def test_startup_cleanup_drops_bots_saved_before_filter(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_participant", match_id="EUW1_5", participant_no=7,
                   puuid=BOT, team_id=200)
        insert_row(con, "match_participant", match_id="EUW1_5", participant_no=1,
                   puuid=A, team_id=100)
        insert_row(con, "player_name", puuid=BOT, name="Jade_Taric bot#BOT", seen_at=1)
        insert_row(con, "player_name", puuid=A, name="Zed#EUW", seen_at=1)
    db.migrate()                                            # upgrade_drop_bots
    with db.connect() as con:
        assert [r["puuid"] for r in con.execute(
            "SELECT puuid FROM match_participant ORDER BY participant_no")] == [A]
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM player_name")] == [A]
    db.migrate()                                            # idempotentne
