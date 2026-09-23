"""Bonus milestone jest powtarzalny (misja "Bonus Milestones (x/2)"), wiec
champion po bonusie 1 dalej liczy sie do zadania - jego celem jest
nastepny bonus, a nie wypadniecie z rankingu (zgloszenie 23.09: Vel'Koz
po bonusie 1 zniknal). Drabinka zna szczebel bonus 1 -> bonus 2; wyzsze
bonusy maja ten sam wymog, wiec brakujacy szczebel powyzej ostatniego
znanego bonusu dziedziczy go zamiast kary za nieznany szczebel."""
from app import scoring

LADDER = {
    0: {"require_grades": {"A-": 1}, "games": 1, "reward_marks": 1, "bonus": False},
    1: {"require_grades": {"A-": 1}, "games": 1, "reward_marks": 1, "bonus": False},
    2: {"require_grades": {"S-": 1}, "games": 1, "reward_marks": 2, "bonus": False},
    3: {"require_grades": {"S-": 1}, "games": 1, "reward_marks": 2, "bonus": False},
    4: {"require_grades": {"S-": 2}, "games": 2, "reward_marks": 1, "bonus": True},
    5: {"require_grades": {"S-": 2}, "games": 2, "reward_marks": 1, "bonus": True},
}
PRIOR = {"A-": 0.5, "S-": 0.2}
GOAL = 5


def _row(cid, milestone, earned=()):
    return {"champion_id": cid, "milestone": milestone, "points": 1000,
            "grades_earned": list(earned)}


def test_champion_after_bonus_targets_next_bonus():
    rows = [_row(1, 5, ["S"])]
    scoring.score_rows(rows, LADDER, {}, PRIOR, GOAL)
    r = rows[0]
    assert r["goal"] == 6 and r["steps_remaining"] == 1
    assert (r["next_grade"], r["next_need"], r["next_have"]) == ("S-", 2, 1)
    assert r["path_known"] is True and r["expected_games"] == 5.0   # (2-1)/0.2


def test_bonus_above_known_ladder_repeats_last_bonus_rung():
    rows = [_row(1, 6)]
    scoring.score_rows(rows, LADDER, {}, PRIOR, GOAL)
    r = rows[0]
    assert r["goal"] == 7 and r["path_known"] is True
    assert r["path"][0]["grade"] == "S-" and r["path"][0]["need"] == 2
    assert r["expected_games"] == 10.0


def test_champion_below_goal_keeps_mission_goal():
    rows = [_row(1, 3)]
    scoring.score_rows(rows, LADDER, {}, PRIOR, GOAL)
    assert rows[0]["goal"] == 5 and rows[0]["steps_remaining"] == 2


def test_ranking_keeps_champions_past_the_goal(fresh_db, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from app import db
    from app import main as app_main
    monkeypatch.setattr(app_main, "GOAL", 5)

    def entry(cid, ms, bonus):
        return {"championId": cid, "championLevel": 20, "championPoints": 90000,
                "lastPlayTime": int(time.time()) * 1000,
                "championSeasonMilestone": ms, "tokensEarned": 0,
                "markRequiredForNextLevel": 2,
                "nextSeasonMilestone": {"requireGradeCounts": {"S-": 2},
                                        "totalGamesRequires": 2, "rewardMarks": 1,
                                        "bonus": bonus},
                "milestoneGrades": []}
    db.save_champions([(161, "Vel'Koz", "Velkoz"), (45, "Veigar", "Veigar")])
    entries = [entry(161, 5, True), entry(45, 4, True)]
    now = int(time.time())
    db.learn_ladder(entries, now)
    db.save_snapshot(now, entries)
    r = TestClient(app_main.app).get("/api/targets?limit=10")
    ids = {t["champion_id"]: t for t in r.json()["targets"]}
    assert 161 in ids, "champion po bonusie 1 zostaje w rankingu"
    assert ids[161]["goal"] == 6 and ids[45]["goal"] == 5
