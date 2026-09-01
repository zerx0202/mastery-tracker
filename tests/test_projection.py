"""Projekcja misji z losowaniem pul (symulacja zamiast dolnej granicy)."""
from app import db, model


def _fake_world(monkeypatch, milestones):
    monkeypatch.setattr(db, "latest_snapshot_id", lambda: 1)
    monkeypatch.setattr(db, "get_ladder", lambda: {
        3: {"require_grades": {"S-": 1}, "games": 1, "reward_marks": 2}})
    monkeypatch.setattr(db, "snapshot_rows", lambda sid: [
        {"champion_id": cid, "milestone": ms} for cid, ms in milestones.items()])
    monkeypatch.setattr(db, "median_final_pool_size", lambda: 2)
    monkeypatch.setattr(model, "champion_rates", lambda mode=None: {
        "champions": {}, "prior": {"S-": 0.5, "A-": 0.6}})


def test_mission_projection_shape(fresh_db, monkeypatch):
    _fake_world(monkeypatch, {1: 3, 2: 3, 3: 3})
    out = model.mission_projection(goal=4, runs=300, seed=7)
    assert out and not out["capped"]
    # p=0.5 na ostatni szczebel -> geometryczna mediana 1-2 gry
    assert 1 <= out["p25"] <= out["median"] <= out["p75"] <= 12
    assert out["pool_size"] == 2 and out["runs"] == 300


def test_mission_projection_needs_world(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "latest_snapshot_id", lambda: None)
    assert model.mission_projection(goal=4) is None
