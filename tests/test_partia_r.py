"""Partia R (7.09): bramki danych po analizach z 4.09 swiecily "otwarte"
bez konca (63/40, 83/60), a Brier liczyl wszystkie pary, gdy E(c) steruje
next_p (4.09: 21 par, z tego 3 z next_p). Bramka wykonana dostaje nowy
prog i notke z werdyktem; Brier liczy pary z next_p."""
from app import db
from tests.conftest import insert_row


def test_gates_carry_rearmed_thresholds_and_notes(fresh_db):
    gates = {g["key"]: g for g in db.data_gates()}
    assert gates["fatigue"]["need"] == 80 and "4.09" in gates["fatigue"]["note"]
    assert gates["class_feats"]["need"] == 100 and "odrzucone" in gates["class_feats"]["note"]
    assert gates["brier"]["need"] == 20 and gates["s_minus"]["need"] == 5
    assert gates["eventdata"]["need"] == 50 and gates["big_review"]["need"] == 100
    assert all(g.get("note") for g in gates.values())


def test_brier_gate_counts_only_pairs_with_next_p(fresh_db):
    ts = 1_700_000_000
    pool = db.save_pool([45, 99], "KIWI", 2400, "limited", ts)
    db.save_pool_predictions(pool, [
        {"champion_id": 45, "next_grade": "A-", "model_p": 0.7, "next_p": None,
         "model_own_games": 3}], ts)                      # tylko model-p
    db.link_pool_to_match("EUW1_1", 45, None, ts + 600)
    pool2 = db.save_pool([12, 99], "KIWI", 2400, "limited", ts + 3600)
    db.save_pool_predictions(pool2, [
        {"champion_id": 99, "next_grade": "S-", "model_p": None, "next_p": 0.12,
         "model_own_games": 1}], ts + 3600)               # tylko next_p (E(c))
    db.link_pool_to_match("EUW1_2", 99, None, ts + 4200)
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1, champion_id=45,
                   grade="A", censored=0, observed_at=ts + 2000)
        insert_row(con, "grade_observation", match_id="EUW1_2", game_id=2, champion_id=99,
                   grade="B", censored=0, observed_at=ts + 5000)
    resolved, _ = db.prediction_pairs()
    assert len(resolved) == 2
    g = {x["key"]: x for x in db.data_gates()}["brier"]
    assert g["have"] == 1 and "wszystkie pary: 2" in g["note"]
