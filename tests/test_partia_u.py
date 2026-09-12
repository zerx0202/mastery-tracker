"""Partia U (12.09): 500 "database is locked" na /lobby, /targets, /live
i pusta tabela w champ selekcie (log 10.09, konsola przegladarki). Odczyty
dashboardu schodza z petli zdarzen (synchroniczny connect() z busy_timeout
10 s zamrazal caly serwer na czas zapisu po grze / kopii backupu),
a zablokowana baza oddaje ostatnia dobra odpowiedz ze stale=True zamiast
500. Snowball commituje per gra, backup.sh kopiuje w krokach."""
import sqlite3
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app, state

SELECT = {"champion_ids": [1, 2, 3], "trade_ids": [], "queue": "KIWI",
          "pool_kind": "limited", "queue_id": 2400, "allies": []}


def _locked():
    raise sqlite3.OperationalError("database is locked")


def test_lobby_serves_last_good_response_when_db_is_locked(fresh_db, monkeypatch):
    from tests.test_partia_f import _world
    _world(int(time.time()))
    state.pop("last_pool_id", None)
    state.pop("last_good", None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/lobby", json=SELECT).status_code == 200
    good = client.get("/api/lobby").json()
    assert good["active"] and "stale" not in good

    monkeypatch.setattr(db, "get_lobby", _locked)
    r = client.get("/api/lobby")
    assert r.status_code == 200 and r.json()["stale"] is True
    assert r.json()["targets"] == good["targets"]
    # inny blad nie jest maskowany ostatnia dobra odpowiedzia
    monkeypatch.setattr(db, "get_lobby", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get("/api/lobby").status_code == 500


def test_live_falls_back_only_after_a_good_response(fresh_db, monkeypatch):
    state.pop("last_good", None)
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(db, "get_live", _locked)
    assert client.get("/api/live").status_code == 500        # nic dobrego w pamieci
    monkeypatch.setattr(db, "get_live", lambda: None)
    assert client.get("/api/live").json() == {"active": False}
    monkeypatch.setattr(db, "get_live", _locked)
    assert client.get("/api/live").json() == {"active": False, "stale": True}


def test_dashboard_reads_still_answer(fresh_db):
    # odczyty przeniesione do watkow maja dawac to samo, co przedtem
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/sentinel").json()["open"] is False
    assert client.get("/api/balance").json() == {}
    assert client.get("/api/missions").json() == {"missions": []}
    assert "tempo" in client.get("/api/pass").json()
    assert client.get("/api/split/progress").status_code == 400   # brak snapshotow
    assert client.get("/api/grades/history").json()["count"] == 0
    assert "gates" in client.get("/api/system/health").json()


def test_snowball_ingest_commits_per_game(fresh_db, monkeypatch):
    commits = []

    class Spy(sqlite3.Connection):
        def commit(self):
            commits.append(1)
            super().commit()

    real = sqlite3.connect
    monkeypatch.setattr(db.sqlite3, "connect",
                        lambda *a, **k: real(*a, factory=Spy, **k))
    games = [{"gameId": 100 + i, "gameMode": "KIWI", "queueId": 2400,
              "gameDuration": 1200, "gameCreation": 1_700_000_000_000,
              "participants": [{"championId": 45, "teamId": 100,
                                "stats": {"kills": i, "assists": 2}}]}
             for i in range(3)]
    kiwi, rows = db.snowball_ingest("p" * 36, games)
    assert (kiwi, rows) == (3, 6)
    assert len(commits) >= 3                                  # co najmniej raz na gre


def test_lobby_paths_never_touch_db_on_the_event_loop(fresh_db, monkeypatch):
    # (przeglad U) klucz cache lobby_targets i zapis w push_lobby siedzialy
    # na petli zdarzen mimo partii U - straznik: db.* wolane z petli = blad
    import asyncio
    from tests.test_partia_f import _world
    _world(int(time.time()))
    state.pop("last_pool_id", None)
    state.pop("last_good", None)

    def off_loop(fn):
        def guard(*a, **k):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return fn(*a, **k)                 # w watku - tak ma byc
            raise AssertionError(f"{fn.__name__} na petli zdarzen")
        return guard
    for name in ("latest_snapshot_id", "get_lobby", "set_lobby",
                 "get_json_setting", "my_lcu_puuid"):
        monkeypatch.setattr(db, name, off_loop(getattr(db, name)))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/lobby", json=SELECT).status_code == 200
    assert client.post("/api/lobby", json=dict(SELECT, champion_ids=[])).status_code == 200
    assert client.get("/api/lobby").status_code == 200
    assert client.get("/api/targets?limit=3").status_code == 200
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/players?puuids=" + "a" * 36).status_code == 200


def test_targets_fallback_only_for_dashboard_calls(fresh_db):
    from tests.test_partia_f import _world
    _world(int(time.time()))
    state.pop("last_good", None)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/targets?limit=3&ids=1,2").status_code == 200
    assert not [k for k in state.get("last_good", {}) if k.startswith("targets")]
    assert client.get("/api/targets?limit=3").status_code == 200
    assert "targets:3:None:None" in state["last_good"]
