"""Partia K (4.09): agent - catch-all WebSocket z rozdzielnia po uri
(subskrypcje per-temat nie dostarczaly zdarzen: 4 dni logow bez oceny
z WS, rotacje na siatce pollingu 10 s), odcisk sesji champ selecta,
sojusznicy z myTeam w /lobby (karta 9, sonda C1), wariant a: odzysk
dokladnej oceny ostatniej gry z notyfikacji przy wykryciu klienta
(sonda C2: retencja = sesja klienta). Backend: kolumna allies, liczniki WS
w zdrowiu agenta."""
import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_under_test_k", Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

NOTIF = {"gameId": 7971058915, "championId": 53, "playerGrade": "B+", "score": 720,
         "championPointsGained": 481, "championPointsGainedIndividualContribution": 96,
         "championPointsBeforeGame": 8166, "championLevel": 3, "championLevelUp": False,
         "tokensEarned": 2, "tokenEarnedAfterGame": False,
         "memberGrades": [{"championId": 143, "grade": "S", "puuid": "e" * 36}]}
EMPTY = {"gameId": 0, "championId": 0, "playerGrade": "", "memberGrades": []}


class FakeServer:
    def __init__(self, new=1):
        self.posts, self.new = [], new

    async def post(self, path, payload=None, timeout=90):
        self.posts.append((path, payload))
        return {"new": self.new, "stored": True}


class FakeLcu:
    port = "1"

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def get(self, path, timeout=8):
        self.calls.append(path)
        return self.responses.get(path)


def _agent(lcu, server=None):
    a = ag.Agent({"api_base": "http://backend"})
    a.lcu, a.server = lcu, server or FakeServer()
    a.pre_snapshot_done = True
    return a


# ---------- wariant a: notyfikacja ----------

def test_notification_maps_to_mastery_update_shape():
    e = ag.notification_to_update(NOTIF)
    assert (e["grade"], e["pointsGained"], e["pointsGainedIndividualContribution"],
            e["pointsBeforeGame"], e["level"], e["hasLeveledUp"]) == ("B+", 481, 96, 8166, 3, False)
    assert e["gameId"] == 7971058915 and e["memberGrades"]           # oryginal zostaje
    assert ag.notification_to_update(EMPTY) is None                    # szkielet po restarcie
    assert ag.notification_to_update(None) is None


def test_recover_last_grade_posts_and_reports_only_new():
    async def run():
        a = _agent(FakeLcu({ag.NOTIFICATIONS: NOTIF}))
        assert await a.recover_last_grade() is True
        path, payload = a.server.posts[-1]
        assert path == "/grade" and payload["updates"][0]["grade"] == "B+"
        # znana ocena: backend oddaje new=0 -> False, bez logu "odzyskana"
        b = _agent(FakeLcu({ag.NOTIFICATIONS: NOTIF}), FakeServer(new=0))
        assert await b.recover_last_grade() is False
        # pusty szkielet po restarcie klienta: zero POST-ow
        c = _agent(FakeLcu({ag.NOTIFICATIONS: EMPTY}))
        assert await c.recover_last_grade() is False and c.server.posts == []
    asyncio.run(run())


# ---------- champ select: odcisk sesji + sojusznicy ----------

def _sess(bench, team):
    return {"benchEnabled": True,
            "benchChampions": [{"championId": c} for c in bench],
            "myTeam": team, "localPlayerCellId": 2}


class GameflowLcu(FakeLcu):
    def __init__(self):
        super().__init__({"/lol-gameflow/v1/session": {
            "gameData": {"queue": {"gameMode": "KIWI", "id": 2400}}}})


def test_champ_select_sends_allies_and_skips_identical_sessions():
    async def run():
        a = _agent(GameflowLcu())
        team = [{"cellId": 0, "championId": 517, "puuid": "", "gameName": "",
                 "nameVisibilityType": "HIDDEN"},
                {"cellId": 1, "championId": 69, "puuid": "a" * 36, "gameName": "Zed",
                 "tagLine": "EUW", "nameVisibilityType": "UNHIDDEN"},
                {"cellId": 2, "championId": 53, "puuid": "m" * 36, "gameName": "Ja",
                 "tagLine": "1", "nameVisibilityType": "UNHIDDEN"}]
        sess = _sess([114, 360], team)
        await a.handle_champ_select(sess)
        await a.handle_champ_select(json.loads(json.dumps(sess)))   # ten sam tik zegara
        posts = [p for p in a.server.posts if p[0] == "/lobby"]
        assert len(posts) == 1                                          # odcisk odsial powtorke
        assert a.lcu.calls.count("/lol-gameflow/v1/session") == 1      # bez drugiego GET-a
        allies = posts[0][1]["allies"]
        assert [x["cellId"] for x in allies] == [0, 1]                  # bez mojej komorki
        assert allies[0]["hidden"] is True and allies[0]["puuid"] == ""
        assert allies[1] == {"cellId": 1, "championId": 69, "puuid": "a" * 36,
                             "name": "Zed#EUW", "hidden": False}
        # wyjscie z champ selecta zeruje odcisk - ta sama sesja pozniej idzie znow
        await a.handle_champ_select(None)
        await a.handle_champ_select(sess)
        assert len([p for p in a.server.posts if p[0] == "/lobby"]) == 3
    asyncio.run(run())


# ---------- catch-all WS: rozdzielnia ----------

def test_dispatch_ws_routes_and_counts(tmp_path):
    async def run():
        a = _agent(GameflowLcu())
        a.server.QUEUE_DIR = tmp_path / "queue"   # _queue_stats liczy pliki kolejki
        assert await a.dispatch_ws("/lol-gameflow/v1/gameflow-phase", ag.PHASE_IN_GAME) is True
        assert a.in_game is True
        sess = _sess([114], [{"cellId": 2, "championId": 53}])
        assert await a.dispatch_ws("/lol-champ-select/v1/session", sess) is True
        assert any(p[0] == "/lobby" for p in a.server.posts)
        assert await a.dispatch_ws(
            "/lol-end-of-game/v1/champion-mastery-updates",
            [{"grade": "A", "championId": 53, "gameId": 5, "pointsGained": 400}]) is True
        assert any(p[0] == "/grade" for p in a.server.posts)
        assert await a.dispatch_ws("/lol-chat/v1/conversations", {"x": 1}) is False
        assert a.ws_events == {"total": 4, "phase": 1, "champ_select": 1, "mastery": 1}
        # meldunek zdrowia niesie liczniki
        await a.report_health()
        health = [p for p in a.server.posts if p[0] == "/agent/health"][-1][1]
        assert health["ws_events"]["total"] == 4
    asyncio.run(run())


# ---------- backend ----------

def test_lobby_and_pool_store_allies(fresh_db):
    from app.main import state
    from tests.test_partia_f import _world
    _world(int(time.time()))                 # snapshot: bez niego /lobby oddaje 400
    state.pop("last_pool_id", None)
    client = TestClient(app, raise_server_exceptions=False)
    body = {"champion_ids": [1, 2, 3], "trade_ids": [], "queue": "KIWI",
            "pool_kind": "limited", "queue_id": 2400,
            "allies": [{"cellId": 1, "championId": 69, "puuid": "a" * 36,
                        "name": "Zed#EUW", "hidden": False}, "smiec"]}
    assert client.post("/api/lobby", json=body).status_code == 200
    assert db.get_lobby()["allies"] == [{"cellId": 1, "championId": 69, "puuid": "a" * 36,
                                         "name": "Zed#EUW", "hidden": False}]
    out = client.get("/api/lobby").json()
    assert out["allies"][0]["name"] == "Zed#EUW"
    with db.connect() as con:
        row = con.execute("SELECT allies FROM champ_select_pool").fetchone()
    assert json.loads(row["allies"])[0]["puuid"] == "a" * 36
    db.migrate()                                                       # idempotentne


def test_agent_health_stores_ws_events(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/agent/health", json={"queue": 0, "bad": 0, "ws_ok": True,
                                           "ws_events": {"total": 12, "phase": 1,
                                                         "champ_select": 9, "mastery": 0}})
    h = client.get("/api/system/health").json()
    assert h["agent_health"]["ws_events"]["champ_select"] == 9
    client.post("/api/agent/health", json={"queue": 0, "bad": 0, "ws_ok": True,
                                           "ws_events": "smiec"})
    assert client.get("/api/system/health").json()["agent_health"]["ws_events"] is None
