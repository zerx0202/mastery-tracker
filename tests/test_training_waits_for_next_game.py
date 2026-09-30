"""Trening po nowej ocenie czeka na start nastepnej gry (30.09). Log agenta
(7/7 gier): seria 500 / "database is locked" zaczyna sie ~15 s po koncu
gry i konczy razem z treningiem (~4 min; w kopii ocena -> koniec treningu
159-211 s). Na Macu (2 vCPU, virtiofs) trening w osobnym procesie zjada
procesor i dysk dokladnie wtedy, gdy idzie zapis po grze i kolejny champ
select. Start gry = champ select za nami, 20 min z samymi drobnymi zapisami
live; bez kolejnej gry trening rusza po TRAIN_IDLE_S ciszy. Ranking liczy
szanse z czestosci ocen, nie z trenowanego modelu - opoznienie go nie rusza."""
import time

from fastapi.testclient import TestClient

from app import db, model
from app import main as app_main

GRADE = {"updates": [{"gameId": 7, "grade": "S-", "championId": 45}]}
LIVE = {"champion_id": 45, "champion": "Veigar", "game_mode": "KIWI",
        "game_time": 60.0, "kills": 0, "deaths": 0, "assists": 0, "cs": 0,
        "ward_score": 0, "gold_est": 500, "level": 1, "raw": {}}


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)
    monkeypatch.setattr(model, "train", lambda *a, **k: calls.append(1) or {"models": {}})
    app_main._TRAIN["pending"] = False
    return calls


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_new_grade_trains_when_next_game_starts(fresh_db, monkeypatch):
    calls = _spy(monkeypatch)
    with TestClient(app_main.app) as c:
        assert c.post("/api/grade", json=GRADE).json()["new"] == 1
        time.sleep(0.5)
        assert calls == [] and app_main._TRAIN["pending"] is True
        c.post("/api/lobby", json={"champion_ids": [], "queue": None,
                                   "pool_kind": None, "queue_id": 0})
        time.sleep(0.3)
        assert calls == [], "champ select to nie moment na trening"
        c.post("/api/live", json=LIVE)
        assert _wait(lambda: calls == [1])
        c.post("/api/live", json=LIVE)          # kolejne odczyty live nie dubluja
        time.sleep(0.3)
        assert calls == [1] and app_main._TRAIN["pending"] is False


def test_pending_training_runs_after_quiet_period(fresh_db, monkeypatch):
    calls = _spy(monkeypatch)

    async def run():
        app_main.defer_training()
        app_main.maybe_train_when_idle()          # swieza aktywnosc - czeka
        assert app_main._TRAIN["pending"] is True
        app_main.state["last_activity"] = time.time() - app_main.TRAIN_IDLE_S - 1
        app_main.maybe_train_when_idle()
        await app_main.training_idle()
    import asyncio
    asyncio.run(run())
    assert calls == [1] and app_main._TRAIN["pending"] is False


def test_agent_activity_postpones_idle_training(fresh_db, monkeypatch):
    _spy(monkeypatch)
    app_main.state["last_activity"] = 0
    c = TestClient(app_main.app)
    c.post("/api/lobby", json={"champion_ids": [], "queue": None,
                               "pool_kind": None, "queue_id": 0})
    assert time.time() - app_main.state["last_activity"] < 5


def test_history_ingest_defers_training_too(fresh_db, monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(db, "save_lcu_game", lambda g, my=None: True)
    monkeypatch.setattr(db, "get_cached_puuid", lambda name: None)
    with TestClient(app_main.app) as c:
        c.post("/api/history/lcu", json={"games": [{"gameId": 1}]})
        time.sleep(0.3)
        assert calls == [] and app_main._TRAIN["pending"] is True
