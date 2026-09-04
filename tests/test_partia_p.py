"""Partia P (4.09): przeglad diffu K-O w kontrakcie cavecrew-reviewer - piec
uwag, cztery wziete wprost, piata (tag BOT) jako zaostrzenie heurystyki.
1. Sojusznicy znikali z panelu live na cala gre: wyjscie z champ selecta to
   pusta pula, REPLACE lobby kasowal allies, /lobby bylo nieaktywne.
2. Dokladna ocena nad wierszem cenzurowanym ze snapshotu wracala jako "nie
   nowa" - bez treningu, zdarzenia i logu odzysku (wariant a).
3. WS Delete sesji champ selecta niesie ostatni stan - odcisk polykal wyjscie.
4. Pusty gameflow zamrazal UNKNOWN/q=0 odciskiem do zmiany sesji."""
import asyncio
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app, state
from tests.conftest import insert_row

A, B = "a" * 36, "b" * 36
ALLY = {"cellId": 1, "championId": 69, "puuid": A, "name": "Zed#EUW", "hidden": False}
SELECT = {"champion_ids": [1, 2, 3], "trade_ids": [], "queue": "KIWI",
          "pool_kind": "limited", "queue_id": 2400}
EXIT = {"champion_ids": [], "trade_ids": [], "queue": None, "pool_kind": None,
        "queue_id": 0, "allies": []}


def test_allies_survive_champ_select_exit_until_next_lobby(fresh_db):
    from tests.test_partia_f import _world
    _world(int(time.time()))                 # snapshot: bez niego /lobby oddaje 400
    state.pop("last_pool_id", None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/lobby", json=dict(SELECT, allies=[ALLY])).status_code == 200
    assert client.post("/api/lobby", json=EXIT).status_code == 200   # agent: send_pool([])
    out = client.get("/api/lobby").json()
    assert out["active"] is False and out["targets"] == []
    assert out["allies"] == [ALLY]                                    # karta 9 zyje w grze
    # nastepny champ select nadpisuje sojusznikow; przeterminowany wiersz
    # nie oddaje nikogo
    bob = dict(ALLY, puuid=B, name="Bob#EUW")
    client.post("/api/lobby", json=dict(SELECT, allies=[bob]))
    assert client.get("/api/lobby").json()["allies"] == [bob]
    db.set_lobby([], None, None, int(time.time()) - 7200, [], [ALLY])
    out = client.get("/api/lobby").json()
    assert out["active"] is False and out.get("allies", []) == []


def test_exact_grade_over_censored_snapshot_row_counts_as_new(fresh_db):
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_7", game_id=7, champion_id=45,
                   grade=">=A-", observed_at=10, source="snapshot_diff", censored=1)
    entry = {"gameId": 7, "grade": "S-", "championId": 45}
    assert db.save_grade(entry, "euw1", 20) is True      # cenzura -> dokladna = nowa
    assert db.save_grade(entry, "euw1", 30) is False     # powtorka dokladnej = nie
    with db.connect() as con:
        row = con.execute("SELECT grade, COALESCE(censored, 0) c FROM grade_observation "
                          "WHERE match_id='EUW1_7'").fetchone()
    assert (row["grade"], row["c"]) == ("S-", 0)


def test_ws_delete_of_champ_select_session_is_an_exit():
    from tests.test_partia_k import GameflowLcu, _agent, _sess

    async def run():
        a = _agent(GameflowLcu())
        sess = _sess([114], [{"cellId": 2, "championId": 53}])
        assert await a.dispatch_ws("/lol-champ-select/v1/session", sess, "Update") is True
        assert a.last_pool_key is not None
        assert await a.dispatch_ws("/lol-champ-select/v1/session", sess, "Delete") is True
        assert a.last_pool_key is None and a._sess_fp is None
        assert [p[1]["champion_ids"] for p in a.server.posts if p[0] == "/lobby"][-1] == []
    asyncio.run(run())


def test_empty_gameflow_does_not_freeze_unknown_queue():
    from tests.test_partia_k import FakeLcu, _agent, _sess

    async def run():
        a = _agent(FakeLcu({}))                           # gameflow chwilowo nie odpowiada
        sess = _sess([114], [{"cellId": 2, "championId": 53}])
        await a.handle_champ_select(sess)
        lobby = [p[1] for p in a.server.posts if p[0] == "/lobby"]
        assert lobby[-1]["queue"] == "UNKNOWN" and a._sess_fp is None
        a.lcu.responses["/lol-gameflow/v1/session"] = {
            "gameData": {"queue": {"gameMode": "KIWI", "id": 2400}}}
        await a.handle_champ_select(sess)                 # ta sama sesja, kolejny tik
        assert a.lcu.calls.count("/lol-gameflow/v1/session") == 2
        lobby = [p[1] for p in a.server.posts if p[0] == "/lobby"]
        assert lobby[-1]["queue"] == "KIWI" and a._sess_fp is not None
        await a.handle_champ_select(sess)                 # teraz odcisk odsiewa
        assert a.lcu.calls.count("/lol-gameflow/v1/session") == 2
    asyncio.run(run())


def test_bot_identity_needs_tag_and_name(fresh_db):
    assert db._bot_identity({"gameName": "Jade_Taric bot", "tagLine": "BOT"})
    assert db._bot_identity({"summonerName": "Annie Bot", "tagLine": "BOT"})
    assert not db._bot_identity({"gameName": "Zed", "tagLine": "BOT"})     # czlowiek z tagiem
    assert not db._bot_identity({"gameName": "Robot", "tagLine": "EUW"})
    with db.connect() as con:
        insert_row(con, "player_name", puuid=A, name="Zed#BOT", seen_at=1)
        insert_row(con, "player_name", puuid=B, name="Jade_Taric bot#BOT", seen_at=1)
    db.migrate()                                          # upgrade_drop_bots
    with db.connect() as con:
        assert [r["puuid"] for r in con.execute("SELECT puuid FROM player_name")] == [A]
