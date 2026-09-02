"""Partia B z przegladu 2.09 (wieczor): prawda o modelu.

B1: scorecard walidowal wylacznie p modelu porzadkowego, a championa wybiera
E(c) na czestosciach champion_rates - drugi slupek next_p + metryki per prog
i per zrodlo. B3: bramka useful mierzyla accuracy@0.5, nieosiagalna dla S-
przy base rate ~0.1 - teraz log-loss skill. B4: cenzurowana obserwacja
o najnizszym ranku wywalala caly trening KeyError-em (realny fold LOO
wczesnej probki). B5: predict() bez mode liczyl z-score z populacji
wszystkich trybow. B7: simulate.py - filtr milestone<GOAL i drabinka z bazy."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, model
from app.db import GRADE_RANK
from app.main import app
from tests.conftest import insert_row


def _load_simulate():
    spec = importlib.util.spec_from_file_location(
        "simulate_under_test",
        Path(__file__).resolve().parents[1] / "tools" / "simulate.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    return sim


# ---------- B4: cenzurowana obserwacja o najnizszym ranku ----------

def test_fit_ordinal_censored_at_lowest_rank_no_crash():
    """Rank rowny minimum wypada z cuts (P(>=min)=1), a galaz cenzurowana
    indeksowala cut_ix[r] wprost - KeyError klal caly fit."""
    a_minus, s_minus = GRADE_RANK["A-"], GRADE_RANK["S-"]
    X = [[0.1], [-0.2], [0.3]]
    specs = [("censored", a_minus), ("exact", a_minus), ("exact", s_minus)]
    cuts = model._cutpoints(specs)
    assert a_minus not in cuts, "warunek testu: najnizszy rank poza cuts"
    beta, alphas = model._fit_ordinal(X, specs, cuts, 1.0, 20, model.LR_ORD)
    assert all(a == a for a in alphas.values())   # skonczone, bez NaN


def test_loo_survives_single_exact_below_a_minus():
    """Repro weryfikatora: jedna dokladna ocena ponizej A-, reszta A-/S-
    i cenzury >=A-. Fold odkladajacy te jedna obserwacje gubil rank A-
    z cuts i _choose_l2/_loo_predictions padaly - model przestawal sie
    odswiezac po cichu az do drugiej oceny ponizej A-."""
    b_plus, a_minus, s_minus = GRADE_RANK["B+"], GRADE_RANK["A-"], GRADE_RANK["S-"]
    X = [[-1.0], [0.2], [0.5], [0.8], [1.5], [0.1], [0.4], [0.9]]
    specs = [("exact", b_plus),                      # jedyna ponizej A-
             ("exact", a_minus), ("exact", a_minus), ("exact", s_minus),
             ("censored", a_minus), ("censored", a_minus),
             ("censored", a_minus), ("censored", s_minus)]
    preds = model._loo_predictions(X, specs, l2=1.0, epochs=15)
    assert preds["A-"], "LOO ma zwrocic predykcje, nie wyjatek"


# ---------- B3: bramka useful na log-loss ----------

def test_useful_gate_uses_log_loss_not_accuracy():
    # acc == baseline (0.8) - stara bramka gasla; log-loss bije baseline
    # (0.244 vs 0.500), a AUC=1.0 - dokladnie profil progu S-
    good = model._threshold_metrics(
        [(0.45, 1), (0.1, 0), (0.1, 0), (0.1, 0), (0.1, 0)])
    assert good["accuracy"] == good["baseline_accuracy"] == 0.8
    assert good["log_loss"] < good["baseline_log_loss"]
    assert good["useful"] is True

    # AUC wysokie, ale p niemal stale ~0.5 - log-loss gorszy od baseline
    bad = model._threshold_metrics(
        [(0.55, 1), (0.45, 0), (0.45, 0), (0.45, 0), (0.45, 0)])
    assert bad["auc"] == 1.0
    assert bad["log_loss"] > bad["baseline_log_loss"]
    assert bad["useful"] is False


# ---------- B1: kalibracja per prog i per zrodlo ----------

def test_calibration_stats_formulas():
    c = model.calibration_stats([(0.5, 1), (0.5, 0)])
    assert c["n"] == 2 and c["brier"] == 0.25 and c["hit_rate"] == 0.5
    assert c["spiegelhalter_z"] is None          # wariancja 0 przy p=0.5

    c = model.calibration_stats([(0.9, 1), (0.8, 1), (0.1, 0)])
    assert c["brier"] == 0.02
    assert c["hit_ci95"][0] <= c["hit_rate"] <= c["hit_ci95"][1]
    assert isinstance(c["spiegelhalter_z"], float)

    assert model.calibration_stats([]) is None


def test_migrate_adds_next_p_column(fresh_db):
    with db.connect() as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(pool_prediction)")]
    assert "next_p" in cols


def test_scorecard_tracks_both_p_sources(fresh_db):
    ts = 1700000000
    pool_id = db.save_pool([45, 99], "KIWI", 2400, "limited", ts)
    # S- z p=None (decyzja: model niewiarygodny nie sugeruje pewnosci),
    # ale next_p z czestosci ISTNIEJE - to on steruje E(c) i jego walidujemy
    db.save_pool_predictions(pool_id, [
        {"champion_id": 45, "next_grade": "S-", "model_p": None,
         "next_p": 0.12, "model_own_games": 3},
        {"champion_id": 99, "next_grade": "A-", "model_p": 0.7,
         "next_p": 0.6, "model_own_games": 1},
    ], ts)
    db.link_pool_to_match("EUW1_500", 45, None, ts + 600)
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_500", game_id=500,
                   champion_id=45, grade="S", observed_at=ts + 900)

    resolved, pending = db.prediction_pairs()
    assert len(resolved) == 1 and pending == 0
    assert resolved[0]["p"] is None and resolved[0]["next_p"] == 0.12

    client = TestClient(app, raise_server_exceptions=False)
    sc = client.get("/api/predictions/scorecard").json()
    assert sc["resolved"] == 1
    rates = sc["per_threshold"]["S-"]["rates"]
    assert rates["n"] == 1 and rates["brier"] == round((0.12 - 1) ** 2, 4)
    assert sc["per_threshold"]["S-"]["model"] is None      # p bylo None
    assert sc["per_threshold"]["A-"]["rates"] is None      # 99 nie zagral


# ---------- B5: predict() z trybem ----------

def test_explain_and_history_pass_mode_to_predict(fresh_db, monkeypatch):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200, game_mode="KIWI")
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="B+", observed_at=1)
    seen = []

    def spy(row, threshold, model=None, baselines=None, mode=None):
        seen.append(mode)
        return {"p": 0.5}
    monkeypatch.setattr(model, "predict", spy)

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/api/model/explain?mode=KIWI")
    client.get("/api/grades/history?mode=KIWI")
    assert seen and set(seen) == {"KIWI"}, \
        "z-score dmg liczyl sie z populacji WSZYSTKICH trybow"


# ---------- B7: simulate.py ----------

def test_simulate_load_state_filters_goal_and_reads_ladder(fresh_db):
    ts = 1700000000
    with db.connect() as con:
        insert_row(con, "snapshot", id=1, taken_at=ts, split_id=1)
        for cid, ms in ((161, 4), (81, 1), (516, 2)):
            insert_row(con, "mastery", snapshot_id=1, champion_id=cid,
                       milestone=ms, points=0, level=1)
        for from_ms, req in ((0, '{"A-": 1}'), (1, '{"A-": 1}'),
                             (2, '{"S-": 1}'), (3, '{"S-": 1}')):
            insert_row(con, "milestone_ladder", from_milestone=from_ms,
                       require_grades=req, games=1, observed_at=ts)
    sim = _load_simulate()
    milestones, rates, prior, ladder = sim.load_state()
    # champion na GOAL wywalal ZeroDivisionError i zerowal wszystkie przebiegi
    assert 161 not in milestones and set(milestones) == {81, 516}
    # drabinka z bazy zamiast hardcode "A- if ms<2": szczebel 2 wymaga S-
    p2 = sim.p_for(516, 2, rates, prior, ladder)
    assert p2 == prior["S-"]
    r = sim.simulate(sim.pick_expected, milestones, rates, prior,
                     pool_size=2, ladder=ladder, runs=5)
    assert len(r) == 5 and all(x >= 1 for x in r)


def test_simulate_weighted_sampler_unique():
    sim = _load_simulate()
    import random
    rnd = random.Random(7)
    pool = sim.sample_weighted([1, 2, 3, 4], [100, 1, 1, 1], 3, rnd)
    assert len(pool) == len(set(pool)) == 3
