"""Jesli model i features policza to samo pole roznie, porownania w apce
klamia. Ten test przybija je do siebie gwozdziem."""
from app import features, model


ROW = {"duration": 900, "gold": 15000, "dmg_champ": 30000,
       "kills": 8, "assists": 12, "deaths": 4, "cs": 60, "champion_id": 1}


def test_match_features_values():
    f = features.match_features(ROW)
    assert f["minutes"] == 15
    assert f["gpm"] == 1000
    assert f["dpm"] == 2000
    assert f["ka_per_min"] == 20 / 15
    assert f["cs_per_min"] == 4


def test_short_game_floor():
    f = features.match_features({"duration": 20, "gold": 500})
    assert f["minutes"] == features.FLOOR_MATCH
    assert f["gpm"] == 500


def test_model_uses_same_arithmetic(fresh_db):
    f = features.match_features(ROW)
    m = model.extract_features(ROW, {}, 1000)
    assert m["gold_per_min"] == f["gpm"]
    assert m["ka_per_min"] == f["ka_per_min"]
    assert m["deaths_per_min"] == f["deaths_per_min"]
    assert m["duration_min"] == f["minutes"]
