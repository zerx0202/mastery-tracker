"""Rytm treningu (03.10). Log agenta 03.10: po przesunieciu treningu na
start gry seria 500 przeszla z okna po grze na pierwsze 4-5 min gry -
trening na Macu (2 vCPU, virtiofs) dlawi serwer niezaleznie od chwili.
Profil (23.09): wybor l2 + walidacja LOO to >80 % czasu, projekcja misji
~15 %, samo dopasowanie ulamek sekundy. Szybki trening = dopasowanie
z ostatnio wybranym l2, walidacja i projekcja z ostatniego pelnego
przebiegu; pelny przebieg co VALIDATE_EVERY nowych ocen (albo recznie).
Model i cechy bez zmian - zmienia sie tylko, jak czesto liczona jest
walidacja."""
import asyncio
import random

from app import db, model
from app import main as app_main
from tests.conftest import insert_row


def _seed(n, start=0, seed=1):
    random.seed(seed + start)
    grades = ["C", "B", "B+", "A-", "A", "S-", "S"]
    with db.connect() as con:
        for i in range(start, start + n):
            k = random.randint(0, 6)
            insert_row(con, "match_player", match_id=f"EUW1_{i}", game_mode="KIWI",
                       duration=1200, champion_id=45 + i % 3, kills=2 + k * 2,
                       deaths=8 - k, assists=5 + k * 3, gold=9000 + k * 900,
                       dmg_champ=12000 + k * 4000, cs=30, game_creation=i)
            insert_row(con, "grade_observation", match_id=f"EUW1_{i}", game_id=i,
                       champion_id=45 + i % 3, grade=grades[k], observed_at=i)


def _spies(monkeypatch):
    calls = {"l2": 0, "loo": 0, "proj": 0}
    real_l2, real_loo = model._choose_l2, model._loo_predictions

    def l2(*a, **k):
        calls["l2"] += 1
        return real_l2(*a, **k)

    def loo(*a, **k):
        calls["loo"] += 1
        return real_loo(*a, **k)
    monkeypatch.setattr(model, "_choose_l2", l2)
    monkeypatch.setattr(model, "_loo_predictions", loo)
    monkeypatch.setattr(model, "mission_projection",
                        lambda *a, **k: calls.__setitem__("proj", calls["proj"] + 1) or None)
    monkeypatch.setattr(model, "EPOCHS_TUNE", 20)
    monkeypatch.setattr(model, "EPOCHS_VAL", 20)
    monkeypatch.setattr(model, "EPOCHS_FINAL", 50)
    return calls


def test_first_training_is_full(fresh_db, monkeypatch):
    _seed(24)
    calls = _spies(monkeypatch)
    out = model.train("KIWI", True, 5, validate=False)   # nic wczesniej - i tak pelny
    # pelny przebieg = 1 wybor l2 (sam robi LOO na kazde l2 z siatki) + 1 LOO
    assert calls == {"l2": 1, "loo": len(model.L2_GRID) + 1, "proj": 1}
    assert out["ordinal"]["validated_on"] == 24


def test_light_training_reuses_last_validation(fresh_db, monkeypatch):
    _seed(24)
    calls = _spies(monkeypatch)
    full = model.train("KIWI", True, 5)
    after_full = dict(calls)
    _seed(3, start=24)
    light = model.train("KIWI", True, 5, validate=False)
    assert calls == after_full, "szybki przebieg bez walidacji i bez projekcji"
    assert light["ordinal"]["trained_on"] == 27
    assert light["ordinal"]["validated_on"] == 24
    assert light["ordinal"]["l2"] == full["ordinal"]["l2"]
    for th in model.THRESHOLDS:
        assert light["models"][th].get("validation") == full["models"][th].get("validation")
        assert "weights" in light["models"][th] or "status" in light["models"][th]


def test_auto_validates_after_enough_new_grades(fresh_db, monkeypatch):
    _seed(24)
    calls = _spies(monkeypatch)
    model.train("KIWI", True, 5)
    _seed(model.VALIDATE_EVERY - 1, start=24)
    model.train("KIWI", True, 5)                          # auto: za malo nowych
    assert calls["l2"] == 1
    _seed(1, start=24 + model.VALIDATE_EVERY - 1)
    out = model.train("KIWI", True, 5)                    # auto: juz VALIDATE_EVERY
    assert calls["l2"] == 2 and calls["proj"] == 2
    assert out["ordinal"]["validated_on"] == 24 + model.VALIDATE_EVERY


def test_forced_validation(fresh_db, monkeypatch):
    _seed(24)
    calls = _spies(monkeypatch)
    model.train("KIWI", True, 5)
    model.train("KIWI", True, 5, validate=True)
    assert calls["l2"] == 2


def test_server_picks_light_at_game_start_and_full_when_idle(fresh_db, monkeypatch):
    seen = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)
    monkeypatch.setattr(model, "train",
                        lambda mode, save, goal, validate=None: seen.append(validate))

    async def run():
        app_main._TRAIN["pending"] = True
        app_main.start_pending_training()                 # start gry: szybki
        await app_main.training_idle()
        app_main.defer_training()
        app_main.state["last_activity"] = 0
        app_main.maybe_train_when_idle()                  # cisza: auto (moze pelny)
        await app_main.training_idle()
        await app_main.run_training(validate=True)        # recznie: pelny
    asyncio.run(run())
    assert seen == [False, None, True]


def test_coalesced_requests_keep_the_strongest_validation(fresh_db, monkeypatch):
    seen = []
    monkeypatch.setattr(app_main, "TRAIN_IN_PROCESS", False)

    def slow(mode, save, goal, validate=None):
        import time
        seen.append(validate)
        time.sleep(0.3)
    monkeypatch.setattr(model, "train", slow)

    async def run():
        app_main.request_training(validate=False)
        await asyncio.sleep(0.05)
        app_main.request_training(validate=False)
        app_main.request_training(validate=None)
        await app_main.training_idle()
    asyncio.run(run())
    assert seen == [False, None]
