"""label_for to serce modelu: cenzurowane ">=X" musi liczyc sie jako trafienie
dla progow ponizej X i jako niewiadoma dla progow powyzej."""
from app import model


def test_label_exact_grades():
    assert model.label_for("B", "A-") == 0
    assert model.label_for("A", "A-") == 1
    assert model.label_for("S+", "S-") == 1
    assert model.label_for("C", "S-") == 0


def test_label_censored_hit():
    assert model.label_for(">=A-", "A-") == 1
    assert model.label_for(">=S-", "A-") == 1
    assert model.label_for(">=S-", "S-") == 1


def test_label_censored_unknown():
    # wiemy tylko, ze >=A- - czy przebilo S-, nie wiadomo
    assert model.label_for(">=A-", "S-") is None
