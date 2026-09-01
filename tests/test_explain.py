"""Karta "czemu taka ocena" (13+27): wklady cech i percentyl obrazen."""
import math

from app import db, model
from tests.conftest import insert_row


def _seed_model():
    db.set_json_setting("grade_model", {
        "features": ["gold_per_min", "ka_per_min", "deaths_per_min",
                     "dmg_ratio", "duration_min"],
        "kind": "ordinal",
        "models": {"A-": {
            "weights": {"gold_per_min": 1.0, "ka_per_min": 0.3,
                        "deaths_per_min": -0.2, "dmg_ratio": 0.5,
                        "duration_min": 0.1},
            "means": [800, 1.5, 0.4, 0.0, 20],
            "stds": [100, 0.5, 0.2, 1.0, 5],
            "bias": -0.2, "status": "dziala",
            "samples": 40, "positives": 20}}})


def test_explain_contributions(fresh_db):
    _seed_model()
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_50", duration=1200,
                   game_mode="KIWI", champion_id=45, kills=8, deaths=4,
                   assists=10, dmg_champ=30000, gold=18000, cs=40,
                   vision=2, heal=0)
        insert_row(con, "grade_observation", match_id="EUW1_50", game_id=50,
                   champion_id=45, grade="A", observed_at=1)
        con.commit()
    ex = model.explain("EUW1_50")
    assert ex["grade"] == "A"
    th = ex["thresholds"]["A-"]
    assert 0 < th["p"] <= 1
    # suma wkladow + bias musi odtwarzac p przez sigmoide
    z = -0.2 + sum(c["pull"] for c in th["contributions"])
    assert abs(1.0 / (1.0 + math.exp(-z)) - th["p"]) < 0.02
    # posortowane po |pull|
    pulls = [abs(c["pull"]) for c in th["contributions"]]
    assert pulls == sorted(pulls, reverse=True)
    assert model.explain("EUW1_BRAK") is None


def test_dmg_percentile_ladder(fresh_db):
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO champion (id, name, key, tags) "
                    "VALUES (45, 'Veigar', 'Veigar', 'Mage')")
        for i in range(10):     # dpm 500..1400 co 100 (duration 600 s)
            gid = 900 + i
            insert_row(con, "snowball_match", game_id=gid, duration=600,
                       game_mode="KIWI", queue_id=2400, game_ts=1,
                       from_puuid="p")
            insert_row(con, "player_stat", match_id=f"SB_{gid}",
                       participant_no=1, champion_id=45, team_id=100,
                       is_local=0, stat_key="totalDamageDealtToChampions",
                       stat_value=(500 + 100 * i) * 10.0)
        con.commit()
    row = {"champion_id": 45, "kills": 0, "deaths": 0, "assists": 0,
           "dmg_champ": 10000, "gold": 0, "cs": 0, "vision": 0, "heal": 0,
           "duration": 600}
    pct = model._dmg_percentile(row, "KIWI")
    assert pct["scope"] == "champion" and pct["n"] == 10
    assert pct["pct"] == 50          # dpm=1000, ponizej lezy 5 z 10
