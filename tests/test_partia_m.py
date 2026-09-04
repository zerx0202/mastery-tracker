"""Partia M (4.09): karta 9 - kto to, wspolna historia, jak poszlo.
Tozsamosci sa od partii D (match_participant, 10 graczy na mecz); tu
dochodza nazwy (player_name z trzech zrodel), podsumowanie per gracz
(razem/przeciw, W/L z mojej perspektywy), rejestr powtarzajacych sie graczy
i znajomi w wierszu oceny. Plus kolumna dmg_mitigated z listingu LCU."""
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row

MY, A, B = "m" * 36, "a" * 36, "b" * 36
NOW = 1_700_000_000


def _block(gid, players):
    """players: [(puuid, 'Nazwa#TAG', team_id, champion_id)] - blok eog."""
    teams = {100: [], 200: []}
    for puuid, name, team, champ in players:
        gn, _, tag = name.partition("#")
        teams[team].append({"puuid": puuid, "riotIdGameName": gn, "riotIdTagLine": tag,
                            "championId": champ, "isLocalPlayer": puuid == MY,
                            "stats": {"kills": 1}})
    return {"gameId": gid, "teams": [{"teamId": t, "players": ps} for t, ps in teams.items()]}


def _game(mid, gid, win, ts, players):
    with db.connect() as con:
        insert_row(con, "match_player", match_id=mid, champion_id=45, duration=1200,
                   game_mode="KIWI", win=win, game_creation=ts * 1000)
    block = _block(gid, players)
    db.save_match_participants(block, mid)
    db.flatten_eog_stats(block, mid)


def test_names_and_shared_history_from_eog_blocks(fresh_db):
    # gra 1: A ze mna (wygrana), B przeciw; gra 2: A przeciw (przegrana)
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 100, 238),
                                (B, "Bob#EUW", 200, 12)])
    _game("EUW1_2", 2, 0, NOW + 3600, [(MY, "Ja#1", 200, 45), (A, "Zed#EUW", 100, 238)])
    s = db.players_summary([A, B, MY], MY)
    assert MY not in s                                   # sam sobie nie jestem graczem
    za = s[A]
    assert (za["name"], za["games"], za["with"], za["against"]) == ("Zed#EUW", 2, 1, 1)
    assert (za["wins_with"], za["wins_against"], za["last_seen"]) == (1, 0, NOW + 3600)
    assert za["recent"][0] == {"match_id": "EUW1_2", "ts": NOW + 3600, "same_team": False,
                               "win": False, "my_grade": None, "their_champion": 238}
    assert s[B]["against"] == 1 and s[B]["wins_against"] == 1
    # biezacy mecz wylaczony = "widziany wczesniej"; B bez innych gier zostaje z nazwa
    s2 = db.players_summary([A, B], MY, exclude_match="EUW1_1")
    assert s2[A]["games"] == 1 and s2[B]["games"] == 0 and s2[B]["name"] == "Bob#EUW"
    assert [p["puuid"] for p in db.recurring_players(MY, min_games=2)] == [A]
    assert db.players_summary([A], None) == {}           # bez mojego puuid nie zgadujemy


def test_names_from_lcu_full_game_and_lobby_allies(fresh_db):
    g = {"gameId": 9, "platformId": "EUW1", "gameMode": "KIWI", "queueId": 2400,
         "participantIdentities": [
             {"participantId": 1, "player": {"puuid": MY, "gameName": "Ja", "tagLine": "1"}},
             {"participantId": 2, "player": {"puuid": A, "gameName": "Zed", "tagLine": "EUW"}}],
         "participants": [
             {"participantId": 1, "teamId": 100, "championId": 45, "stats": {"kills": 1}},
             {"participantId": 2, "teamId": 100, "championId": 238, "stats": {"kills": 2}}]}
    assert db.save_lcu_participants(g, "EUW1_9", MY) == 2
    with db.connect() as con:
        names = {r["puuid"]: r["name"] for r in con.execute("SELECT puuid, name FROM player_name")}
    assert names == {MY: "Ja#1", A: "Zed#EUW"}
    # sojusznik z champ selecta (K) tez zasila nazwy - "kto to" przed gra
    from app.main import state
    from tests.test_partia_f import _world
    _world(int(time.time()))
    state.pop("last_pool_id", None)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/lobby", json={
        "champion_ids": [1, 2], "trade_ids": [], "queue": "KIWI",
        "pool_kind": "limited", "queue_id": 2400,
        "allies": [{"cellId": 1, "championId": 69, "puuid": B, "name": "Bob#EUW",
                    "hidden": False},
                   {"cellId": 3, "championId": 1, "puuid": "", "name": "", "hidden": True}]})
    with db.connect() as con:
        assert con.execute("SELECT name FROM player_name WHERE puuid=?",
                           (B,)).fetchone()["name"] == "Bob#EUW"
        assert con.execute("SELECT COUNT(*) c FROM player_name").fetchone()["c"] == 3


def test_my_lcu_puuid_learned_from_eog_or_derived(fresh_db):
    assert db.my_lcu_puuid() is None
    # (N) puuid z ACCOUNT-V1 (zaszyfrowany, 78 znakow) NIE jest kluczem do
    # danych klienta - nawet zapisany w cache nie ma tu nic do powiedzenia
    db.cache_puuid("Test#EUW", "y" * 78, NOW)
    assert db.my_lcu_puuid() is None
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 100, 238)])
    assert db.my_lcu_puuid() == MY                       # nauczony z isLocalPlayer
    db.set_setting("my_lcu_puuid", "")
    assert db.my_lcu_puuid() == MY                       # wyprowadzony z is_local
    assert db.get_setting("my_lcu_puuid") == MY          # i utrwalony


def test_players_endpoints(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/players?puuids=" + A).json() == {}     # "ja" jeszcze nieznany
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 100, 238)])
    out = client.get("/api/players?puuids=" + A + ",smiec").json()
    assert out[A]["with"] == 1 and out[A]["name"] == "Zed#EUW"
    _game("EUW1_2", 2, 0, NOW + 10, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 200, 238)])
    rec = client.get("/api/players/recurring").json()["players"]
    assert rec[0]["puuid"] == A and rec[0]["against"] == 1 and rec[0]["with"] == 1
    assert client.get("/api/players/recurring?min_games=3").json()["players"] == []


def test_mitigated_column_from_listing_and_startup_backfill(fresh_db):
    g = {"gameId": 5, "platformId": "EUW1", "gameMode": "KIWI", "queueId": 2400,
         "gameCreation": NOW * 1000, "gameDuration": 1200, "gameVersion": "16.17.1",
         "participants": [{"participantId": 3, "championId": 45,
                           "stats": {"win": True, "kills": 1, "damageSelfMitigated": 12345,
                                     "totalDamageTaken": 20000}, "timeline": {}}]}
    assert db.save_lcu_game(g) is True
    with db.connect() as con:
        assert con.execute("SELECT dmg_mitigated FROM match_player WHERE match_id='EUW1_5'"
                           ).fetchone()[0] == 12345.0
        # gra sprzed M: kolumna pusta, wartosc w player_stat (eog) -> backfill przy starcie
        insert_row(con, "match_player", match_id="EUW1_6", champion_id=45, duration=1200,
                   game_mode="KIWI")
        insert_row(con, "player_stat", match_id="EUW1_6", participant_no=4, champion_id=45,
                   is_local=1, stat_key="damageSelfMitigated", stat_value=777)
    db.migrate()
    with db.connect() as con:
        assert con.execute("SELECT dmg_mitigated FROM match_player WHERE match_id='EUW1_6'"
                           ).fetchone()[0] == 777.0
