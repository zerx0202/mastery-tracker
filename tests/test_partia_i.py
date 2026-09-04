"""Partia I (4.09): odzysk zgubionych ocen ze snapshotow.

backfill_grades_from_snapshots odpalal sie wylacznie recznie (ostatnio 29.08)
i dopasowywal mecz sztywnym oknem 7200 s po koncu gry - dwie z czterech
zgubionych gier misji lezaly 10 i 34 h od snapshotu (era snapshotow dobowych).
Regula 1:1 (k nowych ocen championa miedzy dwoma snapshotami <-> k jego gier
w przedziale) na kopii: 58 zdarzen, zero konfliktow, 4 odzyskane. Od teraz
automat: po snapshocie, po /history/lcu, przy starcie. Do tego czujka
"gry misji bez zadnej oceny" - dotychczasowa patrzyla tylko na gry z eog."""
import json
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row

T0 = 1_700_000_000
DAY = 86400


def _snap(con, sid, ts, entries):
    """entries: {champion_id: (grades_earned, milestone)}"""
    insert_row(con, "snapshot", id=sid, taken_at=ts, split_id=1)
    for cid, (grades, ms) in entries.items():
        insert_row(con, "mastery", snapshot_id=sid, champion_id=cid,
                   milestone=ms, points=0, level=1,
                   grades_earned=json.dumps(grades))


def _game(con, mid, cid, start, dur=1400, mode="KIWI"):
    insert_row(con, "match_player", match_id=mid, champion_id=cid,
               game_mode=mode, queue_id=2400, duration=dur,
               game_creation=start * 1000)


def _ladder(con):
    for ms, req in ((0, '{"A-": 1}'), (1, '{"A-": 1}'), (2, '{"S-": 1}')):
        insert_row(con, "milestone_ladder", from_milestone=ms,
                   require_grades=req, games=1, observed_at=T0)


def _grades():
    with db.connect() as con:
        return {r["match_id"]: (r["grade"], r["confidence"]) for r in con.execute(
            "SELECT match_id, grade, confidence FROM grade_observation")}


def test_unique_rule_recovers_grade_outside_window(fresh_db):
    with db.connect() as con:
        _ladder(con)
        # snapshoty dobowe: gra 10 h przed drugim snapshotem, daleko poza 7200 s
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 14 * 3600)
        _snap(con, 2, T0 + DAY, {4: (["B+"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert (out["added"], out["unique"], out["unmatched"]) == (1, 1, 0)
    assert _grades()["EUW1_1"] == ("B+", "unique")
    assert db.backfill_grades_from_snapshots()["skipped_existing"] == 1   # idempotentne


def test_unique_rule_pairs_k_grades_with_k_games_in_order(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        _game(con, "EUW1_2", 4, T0 + 5000)
        _snap(con, 2, T0 + DAY, {4: (["C", "A"], 1)})
    db.backfill_grades_from_snapshots()
    g = _grades()
    assert g["EUW1_1"][0] == "C" and g["EUW1_2"][0] == "A"


def test_milestone_advance_is_censored_grade(fresh_db):
    # Smolder 31.08: ["D"] -> [] z awansem 0 -> 1 = ocena >= A- (prog szczebla 0)
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {901: (["D"], 0)})
        _game(con, "EUW1_1", 901, T0 + 60, dur=948)
        _snap(con, 2, T0 + 1200, {901: ([], 1)})
    db.backfill_grades_from_snapshots()
    assert _grades()["EUW1_1"] == (">=A-", "unique")


def test_mismatch_falls_back_to_window(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)      # dwie gry, jedna ocena - nie 1:1
        _game(con, "EUW1_2", 4, T0 + 5000)      # blizsza snapshotowi, w oknie
        _snap(con, 2, T0 + 5000 + 1400 + 600, {4: (["A"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert out["added"] == 1 and out["unique"] == 0
    assert _grades() == {"EUW1_2": ("A", "window")}


def test_mismatch_outside_window_stays_unmatched(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        _game(con, "EUW1_2", 4, T0 + 5000)
        _snap(con, 2, T0 + DAY, {4: (["A"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert out["added"] == 0 and out["unmatched"] == 1 and _grades() == {}


def test_non_grading_games_are_not_candidates(fresh_db):
    """Practice Tool (17 s) i remake tym samym championem w przedziale nie
    moga zabrac oceny prawdziwej grze - nie daja ocen z definicji."""
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        _game(con, "EUW1_9", 4, T0 + 3000, dur=17, mode="PRACTICETOOL")
        _game(con, "EUW1_8", 4, T0 + 4000, dur=200)
        _game(con, "EUW1_7", 4, T0 + 6000, mode="KIWI_CUSTOM")
        _snap(con, 2, T0 + DAY, {4: (["S"], 1)})
    db.backfill_grades_from_snapshots()
    assert _grades() == {"EUW1_1": ("S", "unique")}


def test_existing_grade_is_never_overwritten(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=4, grade="A+", observed_at=T0 + 2500,
                   confidence="exact")
        _snap(con, 2, T0 + DAY, {4: (["A"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert out["skipped_existing"] == 1 and _grades()["EUW1_1"] == ("A+", "exact")


def test_quiet_run_logs_only_when_something_was_added(fresh_db):
    db.backfill_grades_from_snapshots(quiet=True)
    assert not [e for e in db.recent_events(10) if e["kind"] == "grade_backfill"]
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        _snap(con, 2, T0 + DAY, {4: (["B"], 1)})
    db.backfill_grades_from_snapshots(quiet=True)
    ev = [e for e in db.recent_events(10) if e["kind"] == "grade_backfill"]
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["added"] == 1


def test_migrate_recovers_grades_on_start(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {4: ([], 1)})
        _game(con, "EUW1_1", 4, T0 + 1000)
        _snap(con, 2, T0 + DAY, {4: (["B+"], 1)})
    db.migrate()                                  # upgrade_backfill_grades
    assert _grades()["EUW1_1"][0] == "B+"


def test_ingest_and_snapshot_trigger_backfill(fresh_db, monkeypatch):
    from app import main, model
    calls = []
    monkeypatch.setattr(db, "backfill_grades_from_snapshots",
                        lambda window=7200, quiet=False: calls.append(quiet) or {"added": 0})
    monkeypatch.setattr(db, "save_lcu_game", lambda g, my=None: True)
    monkeypatch.setattr(db, "link_orphan_pools", lambda: 0)
    monkeypatch.setattr(db, "get_cached_puuid", lambda name: None)
    monkeypatch.setattr(model, "train", lambda *a, **k: None)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/history/lcu", json={"games": [{"gameId": 1}]})
    assert calls == [True]

    async def fake_puuid():
        return "p" * 36

    async def fake_get(url):
        return [{"championId": 4, "championLevel": 5, "championPoints": 1000,
                 "lastPlayTime": 0, "championSeasonMilestone": 1,
                 "tokensEarned": 0, "markRequiredForNextLevel": 2,
                 "nextSeasonMilestone": {"requireGradeCounts": {"A-": 1},
                                         "totalGamesRequires": 1,
                                         "rewardMarks": 1, "bonus": False},
                 "milestoneGrades": []}]
    monkeypatch.setattr(main, "my_puuid", fake_puuid)
    monkeypatch.setattr(main, "riot_get", fake_get)
    r = client.post("/api/snapshot")
    assert r.status_code == 200 and calls == [True, True]


def test_games_without_grade_sentry(fresh_db):
    now = int(time.time())
    with db.connect() as con:
        _snap(con, 1, now - 5 * DAY, {4: ([], 1)})          # start sledzenia
        _game(con, "EUW1_old", 4, now - 6 * DAY)            # sprzed sledzenia - nie liczy
        _game(con, "EUW1_1", 4, now - 2 * DAY)              # gra misji bez oceny - liczy
        _game(con, "EUW1_2", 14, now - DAY, mode="KIWI_CUSTOM")   # custom - nie
        _game(con, "EUW1_3", 4, now - 600, dur=300)         # skonczona 5 min temu - luz 30 min
        _game(con, "EUW1_4", 4, now - 3 * DAY)              # z ocena
        insert_row(con, "grade_observation", match_id="EUW1_4", game_id=4,
                   champion_id=4, grade="A", observed_at=now - 3 * DAY)
    p = db.pipeline_sanity()
    assert p["games_without_grade"] == 1
    assert db.games_without_grade_ids() == ["EUW1_1"]
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["pipeline_detail"]["games_without_grade"] == ["EUW1_1"]
