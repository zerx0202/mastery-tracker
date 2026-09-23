"""Trening modelu poza sciezka zadania i poza procesem serwera (diagnoza
23.09): model.train to ~1 min czystego Pythona, a w watku serwera glodzil
wszystkie inne zapytania do bazy (konwoj GIL, 0,03 s -> 45 s na kopii)
i trzymal odpowiedz /grade ponad 90 s - agent czekal, kanal WS stal,
champ select szedl z opoznieniem. /grade i /history/lcu odpowiadaja od
razu, trening leci w tle (jeden naraz, zlecenia w trakcie scalane)."""
import asyncio
import threading
import time

from fastapi.testclient import TestClient

from app import db, model
from app import main as app_main

GRADE = {"updates": [{"gameId": 7, "grade": "S-", "championId": 45}]}


def _slow_train(calls, seconds):
    def train(*a, **k):
        calls.append(time.time())
        time.sleep(seconds)
        return {"models": {}}
    return train


def test_grade_answers_before_training_finishes(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)
    monkeypatch.setattr(model, "train", _slow_train(calls, 3))
    with TestClient(app_main.app) as client:
        t0 = time.time()
        r = client.post("/api/grade", json=GRADE)
        assert r.status_code == 200 and r.json()["new"] == 1
        assert time.time() - t0 < 1.5, "odpowiedz czekala na trening"
        deadline = time.time() + 10
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        assert calls, "trening mial ruszyc w tle"


def test_history_ingest_answers_before_training_finishes(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)
    monkeypatch.setattr(model, "train", _slow_train(calls, 3))
    monkeypatch.setattr(db, "save_lcu_game", lambda g, my=None: True)
    monkeypatch.setattr(db, "get_cached_puuid", lambda name: None)
    with TestClient(app_main.app) as client:
        t0 = time.time()
        r = client.post("/api/history/lcu", json={"games": [{"gameId": 1}]})
        assert r.status_code == 200 and r.json()["new"] == 1
        assert time.time() - t0 < 1.5, "odpowiedz czekala na trening"


def test_training_requests_coalesce(fresh_db, monkeypatch):
    # piec zlecen w trakcie jednego treningu = jeden dodatkowy przebieg,
    # nie piec - po grze /grade i /history/lcu strzelaja jeden po drugim
    calls = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)
    monkeypatch.setattr(model, "train", _slow_train(calls, 0.5))

    async def run():
        app_main.request_training()
        await asyncio.sleep(0.1)
        for _ in range(5):
            app_main.request_training()
        await app_main.training_idle()
    asyncio.run(run())
    assert len(calls) == 2


def test_training_failure_is_logged(fresh_db, monkeypatch):
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)

    def boom(*a, **k):
        raise RuntimeError("zly dzien")
    monkeypatch.setattr(model, "train", boom)

    async def run():
        app_main.request_training()
        await app_main.training_idle()
    asyncio.run(run())
    ev = [e for e in db.recent_events(10) if e["kind"] == "model_train_fail"]
    assert ev and "zly dzien" in str(ev[0]["detail"])


def test_training_runs_in_separate_process(fresh_db, monkeypatch):
    # atrapa w procesie serwera NIE moze zostac wywolana - trening idzie
    # w procesie potomnym na tej samej bazie i sam zapisuje grade_model
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", True)
    parent_calls = []
    monkeypatch.setattr(model, "train", lambda *a, **k: parent_calls.append(1))
    main_thread = threading.get_ident()

    async def run():
        assert threading.get_ident() == main_thread
        return await app_main.run_training()
    try:
        out = asyncio.run(run())
    finally:
        app_main.trainer.shutdown()
    assert parent_calls == []
    assert isinstance(out, dict) and "models" in out
    assert db.get_json_setting("grade_model")["mode"] == app_main.DEFAULT_MODE
