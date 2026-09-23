"""Agent nie czeka na serwer w kanale zdarzen i nie gubi sygnalow konca
(diagnoza 23.09, log agenta z 24 gier): po grze serwer wisial minutami,
a obsluga WS czekala inline na POST /lobby, /snapshot i /grade (limit
90 s) - champ select szedl z opoznieniem, WS zrywal sie co gre. Pusta pula
("wyjscie z champ selecta") i /live {ended} nie byly ponawiane - stare
lobby wisialo do 90 min. Snowball chodzil w kolejce i tuz po grze."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_never_waits", Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)


class ScriptedServer:
    """post() per sciezka: lista krokow - liczba = opoznienie w s i sukces,
    None = porazka (brak polaczenia/5xx). Po wyczerpaniu listy sukces."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.posts = []

    async def post(self, path, payload=None, timeout=90):
        steps = self.script.get(path) or []
        step = steps.pop(0) if steps else 0
        if step is None:
            return None
        await asyncio.sleep(step)
        self.posts.append((path, payload))
        return {"new": 1, "stored": True}


class GameflowLcu:
    port = "1"

    async def get(self, path, timeout=8):
        if path == "/lol-gameflow/v1/session":
            return {"gameData": {"queue": {"gameMode": "KIWI", "id": 2400}}}
        return None


def _agent(server):
    a = ag.Agent({"api_base": "http://backend", "snowball": "on"})
    a.lcu = GameflowLcu()
    a.server = server
    return a


def _sess(bench, team, me_cell=0):
    return {"benchEnabled": True,
            "benchChampions": [{"championId": c} for c in bench],
            "myTeam": [{"cellId": i, "championId": c} for i, c in enumerate(team)],
            "localPlayerCellId": me_cell}


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(ag, "POOL_RETRY_FIRST", 0.01)
    monkeypatch.setattr(ag, "POOL_RETRY_MAX", 0.02)
    monkeypatch.setattr(ag, "LIVE_END_RETRY_FIRST", 0.01)


def test_champ_select_event_does_not_wait_for_server():
    async def run():
        a = _agent(ScriptedServer({"/lobby": [5], "/snapshot": [5]}))
        await asyncio.wait_for(
            a.dispatch_ws("/lol-champ-select/v1/session", _sess([10], [20, 21])), 0.5)
        return a
    a = asyncio.run(run())
    assert a.last_pool_key is not None


def test_grade_event_does_not_wait_for_server():
    async def run():
        a = _agent(ScriptedServer({"/grade": [5]}))
        a._grade_done = False
        grade = [{"grade": "A", "championId": 53, "gameId": 5, "pointsGained": 400}]
        await asyncio.wait_for(
            a.dispatch_ws("/lol-end-of-game/v1/champion-mastery-updates", grade), 0.5)
        # epizod pomeczowy przestaje dopytywac od razu, nie po POST-cie
        assert a._grade_done is True
        await a.settle()
        return a
    a = asyncio.run(run())
    assert [p for p, _ in a.server.posts] == ["/grade"]


def test_pool_sends_latest_state_and_retries_until_delivered():
    async def run():
        a = _agent(ScriptedServer({"/lobby": [None, None]}))
        a.pre_snapshot_done = True
        await a.handle_champ_select(_sess([10], [20, 21]))
        await a.handle_champ_select(_sess([10, 11], [20, 21]))   # nowsza pula
        await a.settle()
        return a
    a = asyncio.run(run())
    lobby = [p for path, p in a.server.posts if path == "/lobby"]
    assert lobby and lobby[-1]["champion_ids"] == [10, 11, 20, 21]
    assert all(p["champion_ids"] != [10, 20, 21] for p in lobby[1:]), \
        "starsza pula nie moze przyjsc po nowszej"


def test_pool_exit_is_retried_until_delivered():
    async def run():
        a = _agent(ScriptedServer())
        a.pre_snapshot_done = True
        await a.handle_champ_select(_sess([10], [20, 21]))
        await a.settle()
        a.server.script["/lobby"] = [None, None, None]
        await a.handle_champ_select(None)
        await a.settle()
        return a
    a = asyncio.run(run())
    assert a.server.posts[-1][0] == "/lobby"
    assert a.server.posts[-1][1]["champion_ids"] == []


def test_live_end_is_retried_until_delivered():
    async def run():
        a = _agent(ScriptedServer({"/live": [None, None]}))
        await a._send_live_end()
        return a
    a = asyncio.run(run())
    assert a.server.posts == [("/live", {"ended": True})]


def test_live_end_gives_way_to_new_game():
    # nowa gra nadpisuje wiersz live sama - spozniony "ended" by go skasowal
    async def run():
        a = _agent(ScriptedServer({"/live": [None, None, None]}))
        a._live_active = True
        await a._send_live_end()
        return a
    a = asyncio.run(run())
    assert a.server.posts == []


@pytest.mark.parametrize("phase,allowed", [
    ("None", True), ("Lobby", True), ("Matchmaking", False),
    ("ReadyCheck", False), ("ChampSelect", False), ("InProgress", False),
    ("EndOfGame", False), (None, False)])
def test_background_work_only_when_client_idle(phase, allowed):
    a = _agent(ScriptedServer())
    a.phase = phase
    assert a.background_allowed() is allowed


def test_background_waits_for_post_game_episode():
    async def run():
        a = _agent(ScriptedServer())
        a.phase = "Lobby"
        a._eog_task = asyncio.create_task(asyncio.sleep(0.2))
        busy = a.background_allowed()
        await a._eog_task
        return busy, a.background_allowed()
    assert asyncio.run(run()) == (False, True)


def test_snowball_skips_while_searching_for_game():
    async def run():
        a = _agent(ScriptedServer())
        a.phase = "Matchmaking"
        a.session = None      # dotkniecie sesji = test pada
        await a._snowball_once()
        return a
    a = asyncio.run(run())
    assert a.server.posts == []
