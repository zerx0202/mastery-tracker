"""Silnik porzadkowy: progi maja rosnac po drabince, a P(>=A-) >= P(>=S-)
ma zachodzic z konstrukcji - dwa niezalezne modele tego nie gwarantowaly."""
import random

from app import model
from app.db import GRADE_RANK


def _synth(n=24, seed=3):
    random.seed(seed)
    X, specs = [], []
    for _i in range(n):
        x = [random.gauss(0, 1) for _ in range(3)]
        lat = 1.5 * x[0] + random.gauss(0, 0.7)
        if lat < -0.8:
            specs.append(("exact", GRADE_RANK["C"]))
        elif lat < 0.2:
            specs.append(("exact", GRADE_RANK["B"]))
        elif lat < 1.0:
            specs.append(("censored", GRADE_RANK["A-"]))
        else:
            specs.append(("censored", GRADE_RANK["S-"]))
        X.append(x)
    return X, specs


def test_parse_grade():
    assert model._parse_grade("B+") == ("exact", GRADE_RANK["B+"])
    assert model._parse_grade(">=A-") == ("censored", GRADE_RANK["A-"])
    assert model._parse_grade(">= S-") == ("censored", GRADE_RANK["S-"])
    assert model._parse_grade("X") is None and model._parse_grade(None) is None


def test_cutpoints_drop_min_keep_mission():
    X, specs = _synth()
    cuts = model._cutpoints(specs)
    assert min(r for _, r in specs) not in cuts          # P(>=min) = 1
    assert GRADE_RANK["A-"] in cuts and GRADE_RANK["S-"] in cuts


def test_alphas_monotone_and_threshold_order():
    X, specs = _synth()
    Xs, means, stds = model._standardize(X)
    cuts = model._cutpoints(specs)
    beta, alphas = model._fit_ordinal(Xs, specs, cuts, 1.0, 400, model.LR_ORD)
    ordered = [alphas[c] for c in sorted(alphas)]
    assert all(a < b for a, b in zip(ordered, ordered[1:], strict=False))
    for _ in range(50):
        x = [random.gauss(0, 1.5) for _ in range(3)]
        pa = model._p_ge(x, beta, alphas, GRADE_RANK["A-"])
        ps = model._p_ge(x, beta, alphas, GRADE_RANK["S-"])
        assert pa >= ps


def test_censored_pull_information():
    # cenzurowane >=S- na wysokim x0 maja podniesc szanse S- przy duzym x0
    X, specs = _synth(n=30, seed=5)
    Xs, means, stds = model._standardize(X)
    cuts = model._cutpoints(specs)
    beta, alphas = model._fit_ordinal(Xs, specs, cuts, 1.0, 400, model.LR_ORD)
    hi = model._p_ge([2.0, 0, 0], beta, alphas, GRADE_RANK["S-"])
    lo = model._p_ge([-2.0, 0, 0], beta, alphas, GRADE_RANK["S-"])
    assert hi > lo


def test_auc_ci_sane():
    se, ci = model._auc_ci(0.838, 20, 16)
    assert se and 0.05 < se < 0.09
    assert 0.0 <= ci[0] < 0.838 < ci[1] <= 1.0


def test_train_empty_db_does_not_crash(fresh_db):
    out = model.train(mode="KIWI")
    for th in model.THRESHOLDS:
        assert out["models"][th]["status"] == "za malo danych albo brak obu klas"
