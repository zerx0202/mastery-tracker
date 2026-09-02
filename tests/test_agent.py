"""Harness logiki agenta — bez zywego LCU (raport 2.09, P2).

agent/agent.py nie jest pakietem, wiec ladujemy go po sciezce. Zalezy od
aiohttp (requirements agenta) - przy braku testy pomijaja sie same, ale
CI instaluje aiohttp jawnie, zeby ta warstwa nie byla martwa.

Pokrycie: ekstrakcja puuid, klucz dedup puli z rotacja, kolejka dyskowa
(zapis przy awarii, pomijanie 4xx, dosylka, zatruta glowa - bug z raportu),
submit_grade bez pola grade."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_under_test",
    Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
MY = "99999999-9999-9999-9999-999999999999"


class FakeResp:
    def __init__(self, status, text="{}"):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text


class FakeSession:
    """Kazde post() zdejmuje kolejny scenariusz z listy: int = status HTTP,
    Exception = awaria polaczenia."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(url)
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        return FakeResp(step)


class FakeServer:
    """Rejestrator POST-ow do testow logiki champ selecta."""

    def __init__(self):
        self.posts = []

    async def post(self, path, payload=None, timeout=90):
        self.posts.append((path, payload))
        return {}


def make_server(tmp_path, script):
    srv = ag.Server({"api_base": "http://backend"}, FakeSession(script))
    srv.QUEUE_DIR = tmp_path / "queue"
    return srv


# ---------- puuidy ----------

def test_puuids_from_text_dedup_and_self():
    a = ag.Agent({"api_base": "http://backend"})
    text = json.dumps({"players": [
        {"puuid": U1}, {"puuid": MY}, {"puuid": U2}, {"puuid": U1}]})
    assert a._puuids_from_text(text, my_puuid=MY) == [U1, U2]


# ---------- klucz dedup puli ----------

def _sess(bench, team, me_cell=0):
    return {
        "benchEnabled": True,
        "benchChampions": [{"championId": c} for c in bench],
        "myTeam": [{"cellId": i, "championId": c} for i, c in enumerate(team)],
        "localPlayerCellId": me_cell,
    }


def _pool_agent():
    a = ag.Agent({"api_base": "http://backend"})

    class _Lcu:
        port = "1"

        async def get(self, path, timeout=8):
            return {"gameData": {"queue": {"gameMode": "KIWI", "id": 2400}}}

    a.lcu = _Lcu()
    a.server = FakeServer()
    a.pre_snapshot_done = True   # snapshot to osobny tor, nie ten test
    return a


def test_pool_dedup_and_bench_rotation():
    async def run():
        a = _pool_agent()
        # lawka {10,11}, ja gram 20, sojusznicy 21 i 22
        await a.handle_champ_select(_sess([10, 11], [20, 21, 22]))
        await a.handle_champ_select(_sess([10, 11], [20, 21, 22]))  # bez zmian
        # rotacja sojusznika z lawka: 21 <-> 10, unia puli identyczna
        await a.handle_champ_select(_sess([21, 11], [20, 10, 22]))
        return a.server.posts

    posts = asyncio.run(run())
    lobby = [p for path, p in posts if path == "/lobby"]
    assert len(lobby) == 2, "dedup ma polknac powtorke, ale NIE rotacje"
    assert lobby[0]["champion_ids"] == lobby[1]["champion_ids"]
    assert lobby[0]["trade_ids"] == [21, 22]
    assert lobby[1]["trade_ids"] == [10, 22]


def test_pool_exit_clears_state():
    async def run():
        a = _pool_agent()
        await a.handle_champ_select(_sess([10], [20, 21]))
        await a.handle_champ_select(None)
        assert a.last_pool_key is None
        return a.server.posts

    posts = asyncio.run(run())
    assert posts[-1][0] == "/lobby" and posts[-1][1]["champion_ids"] == []


# ---------- kolejka dyskowa ----------

def test_network_error_enqueues_durable(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("brak sieci")])
        await srv.post("/grade", {"updates": [{"grade": "A-"}]})

    asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 1
    item = json.loads(files[0].read_text(encoding="utf-8"))
    assert item["path"] == "/grade"


def test_4xx_on_live_post_not_enqueued(tmp_path):
    async def run():
        srv = make_server(tmp_path, [401])
        await srv.post("/grade", {"updates": []})

    asyncio.run(run())
    assert not list((tmp_path / "queue").glob("*.json"))


def test_flush_delivers_and_consumes(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), 200])
        await srv.post("/eog", {"block": {}})
        await srv._flush_once()

    asyncio.run(run())
    assert not list((tmp_path / "queue").glob("*.json"))


def test_flush_poisoned_head_does_not_block_queue(tmp_path):
    """Bug z raportu (P1): element odrzucony 4xx stawal w glowie kolejki
    i nic za nim nigdy nie wychodzilo. Po fixie: 4xx -> .bad, reszta idzie."""
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), ConnectionError("x"),
                                     422, 200])
        # dwa wpisy w tej samej milisekundzie - kiedys nadpisywaly sie
        # w kolejce (nazwa = sam timestamp ms); licznik w nazwie to lata
        await srv.post("/grade", {"updates": [{"zly": "ksztalt"}]})
        await srv.post("/eog", {"block": {"gameId": 7}})
        await srv._flush_once()

    asyncio.run(run())
    q = tmp_path / "queue"
    assert not list(q.glob("*.json")), "zdrowy element ma wyjsc mimo zatrutej glowy"
    bad = list(q.glob("*.bad"))
    assert len(bad) == 1, "odrzucony 4xx ma zostac odlozony jako .bad"


def test_flush_stops_on_server_down(tmp_path):
    async def run():
        srv = make_server(tmp_path, [ConnectionError("x"), ConnectionError("x"),
                                     ConnectionError("wciaz lezy")])
        await srv.post("/grade", {"updates": []})
        await srv.post("/eog", {"block": {}})
        await srv._flush_once()
        return srv.session.calls

    calls = asyncio.run(run())
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 2, "przy lezacym serwerze kolejka czeka w spokoju"
    assert len(calls) == 3, "dosylka ma stanac na pierwszym bledzie, nie mielic reszty"


# ---------- ocena ----------

def test_submit_grade_ignores_entries_without_grade():
    async def run():
        a = ag.Agent({"api_base": "http://backend"})
        a.server = FakeServer()
        sent = await a.submit_grade([{"championId": 45}], "poll")
        return sent, a.server.posts

    sent, posts = asyncio.run(run())
    assert sent is False and posts == []
