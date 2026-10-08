"""Przepustka odswiezana w trakcie sesji (08.10). sync_pass szedl tylko przy
wykryciu klienta (event-hub jeszcze pusty) i po grze (klient juz zamykany) -
caly dzien bez zapisu, UI pokazywal stary akt "za 0 dni". Porazka ma byc
widoczna i ponawiana szybko, sukces odswiezany co 30 min."""
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp z requirements agenta")

_spec = importlib.util.spec_from_file_location(
    "agent_pass_refresh", Path(__file__).resolve().parents[1] / "agent" / "agent.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class Lcu:
    port = "1"

    def __init__(self, events):
        self.events = events

    async def get(self, path, timeout=8):
        if path == "/lol-event-hub/v1/events":
            return self.events
        return None


class Server:
    def __init__(self):
        self.posts = []

    async def post(self, path, payload=None, timeout=90):
        self.posts.append((path, payload))
        return {"stored": True}


def _agent(events):
    a = ag.Agent({"api_base": "http://backend"})
    a.lcu, a.server = Lcu(events), Server()
    return a


def test_empty_event_hub_is_a_failure_retried_soon():
    a = _agent(None)
    assert asyncio.run(a.sync_pass()) is False
    assert a.server.posts == []
    assert ag.pass_next_delay(False) == ag.PASS_RETRY < ag.pass_next_delay(True)


def test_new_act_is_sent_and_ended_act_dropped():
    a = _agent([
        {"eventId": "a1", "eventName": "Season 3: Act I", "endDate": _iso(-0.5)},
        {"eventId": "a2", "eventName": "Season 3: Act II", "endDate": _iso(70)},
        {"eventId": "ev", "eventName": "Evergreen", "endDate": "2099-01-01T00:00:00Z"}])
    assert asyncio.run(a.sync_pass()) is True
    (path, payload), = a.server.posts
    assert path == "/pass"
    assert [e["name"] for e in payload["events"]] == ["Season 3: Act II"]


def test_refresh_runs_every_half_hour_after_success():
    assert ag.pass_next_delay(True) == ag.PASS_SYNC_EVERY == 1800
