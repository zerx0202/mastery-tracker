"""Decyzje po bramkach z 23.09 (kopia 21:48): hipoteza zmeczenia zamknieta
(brak sygnalu przy 195 grach w sesjach), zbieranie eventdata zostaje mimo
braku sygnalu w protokole, notki bramek niosa werdykty 23.09."""
from app import db


def test_fatigue_hypothesis_is_closed(fresh_db):
    keys = [g["key"] for g in db.data_gates()]
    assert "fatigue" not in keys
    assert keys == ["s_minus", "brier", "eventdata", "class_feats", "big_review"]


def test_eventdata_collection_stays_by_decision(fresh_db):
    note = {g["key"]: g for g in db.data_gates()}["eventdata"]["note"]
    assert "zbieranie zostaje" in note and "23.09" in note


def test_gate_notes_carry_latest_verdicts(fresh_db):
    gates = {g["key"]: g for g in db.data_gates()}
    for key in ("brier", "eventdata", "class_feats", "big_review"):
        assert "23.09" in gates[key]["note"], key


def test_rearmed_thresholds_double_the_sample(fresh_db):
    # (23.09, "progi tak") powtorka dopiero przy ~podwojonej probie - mniejsze
    # przyrosty nie ruszaja miar (AUC < 0,1 to szum); S- to prog
    # wiarygodnosci, nie powtorka
    need = {g["key"]: g["need"] for g in db.data_gates()}
    assert need == {"s_minus": 5, "brier": 300, "eventdata": 400,
                    "class_feats": 400, "big_review": 400}
    note = {g["key"]: g for g in db.data_gates()}["class_feats"]["note"]
    assert "90 %" in note
