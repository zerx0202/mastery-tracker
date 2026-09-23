"""Tempo snowballa (23.09, decyzja: 1 gracz / 10 s, tylko przy bezczynnym
kliencie). Przy 1/min seria 3 dni x 10 gier (~960 graczy z rewizjami)
potrzebowala ~16 h bezczynnego klienta. Riot nie publikuje limitu LCU dla
historii, wiec zabezpieczeniem jest hamowanie: kazda porazka podwaja
przerwe do 5 min, sukces wraca do 10 s, pusta kolejka = zapytanie do
serwera raz na minute. Porazka LCU nie moze tez oznaczyc gracza jako
sprawdzonego pustym ingestem (wypadal z kolejki na 7 dni)."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_snowball_pace", Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

PU = "11111111-1111-1111-1111-111111111111"


class NextResp:
    def __init__(self, puuids):
        self.puuids = puuids

    async def json(self):
        return {"puuids": self.puuids}


class Session:
    def __init__(self, puuids):
        self.puuids = puuids

    async def get(self, url, timeout=None):
        return NextResp(self.puuids)


class Lcu:
    port = "1"

    def __init__(self, history):
        self.history = history

    async def get(self, path, timeout=8):
        return self.history


class Server:
    def __init__(self, ok=True):
        self.ok = ok
        self.posts = []

    async def post(self, path, payload=None, timeout=90):
        self.posts.append((path, payload))
        return {"kiwi": 1, "new_rows": 10} if self.ok else None


def _agent(puuids=(PU,), history=None, server_ok=True, phase="None"):
    a = ag.Agent({"api_base": "http://backend", "snowball": "on"})
    a.session = Session(list(puuids))
    a.lcu = Lcu(history if history is not None else {"games": {"games": [{"gameId": 1}]}})
    a.server = Server(server_ok)
    a.phase = phase
    return a


def test_processed_player_means_next_in_ten_seconds():
    a = _agent()
    assert asyncio.run(a._snowball_once()) == "done"
    assert a.server.posts[0][0] == "/snowball/ingest"
    assert ag.snowball_next_delay("done", 60) == ag.SNOWBALL_INTERVAL == 10


def test_empty_queue_or_busy_client_polls_once_a_minute():
    assert asyncio.run(_agent(puuids=())._snowball_once()) == "idle"
    assert asyncio.run(_agent(phase="Matchmaking")._snowball_once()) == "idle"
    assert ag.snowball_next_delay("idle", 10) == ag.SNOWBALL_IDLE_POLL == 60


def test_lcu_failure_does_not_mark_player_checked():
    a = _agent(history=None)
    a.lcu.history = None
    assert asyncio.run(a._snowball_once()) == "fail"
    assert a.server.posts == [], "pusty ingest oznaczylby gracza jako sprawdzonego"


def test_server_failure_counts_as_failure():
    assert asyncio.run(_agent(server_ok=False)._snowball_once()) == "fail"


def test_failures_back_off_to_five_minutes_and_success_resets():
    d = ag.SNOWBALL_INTERVAL
    seen = []
    for _ in range(8):
        d = ag.snowball_next_delay("fail", d)
        seen.append(d)
    assert seen[:4] == [20, 40, 80, 160]
    assert max(seen) == ag.SNOWBALL_BACKOFF_MAX == 300
    assert ag.snowball_next_delay("done", d) == 10
