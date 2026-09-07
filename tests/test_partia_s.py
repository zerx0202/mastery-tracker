"""Partia S (7.09, kopia 7.09): jedyne "unmatched" backfillu to gra, ktorej
papierowy koniec (game_creation + duration) wypadl ~1 min PRZED poprzednim
snapshotem bez oceny - ekran ladowania nie wchodzi w duration. Luz 60 s
-> SNAPSHOT_SLACK_S = 300; hipoteza "eog-only bez game_creation" odrzucona
(0 takich wierszy na kopii)."""
from app import db
from tests.test_partia_i import DAY, T0, _game, _grades, _ladder, _snap


def test_paper_end_just_before_previous_snapshot_is_still_a_candidate(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {53: ([], 1)})                 # snapshot BEZ oceny...
        _game(con, "EUW1_1", 53, T0 - 1400 - 200)        # ...gra "skonczona" 200 s wczesniej
        _snap(con, 2, T0 + DAY, {53: (["B+"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert (out["added"], out["unique"], out["unmatched"]) == (1, 1, 0)
    assert _grades()["EUW1_1"] == ("B+", "unique")


def test_slack_has_a_limit(fresh_db):
    with db.connect() as con:
        _ladder(con)
        _snap(con, 1, T0, {53: ([], 1)})
        _game(con, "EUW1_1", 53, T0 - 1400 - db.SNAPSHOT_SLACK_S - 60)   # poza luzem
        _snap(con, 2, T0 + DAY, {53: (["B+"], 1)})
    out = db.backfill_grades_from_snapshots()
    assert (out["added"], out["unmatched"]) == (0, 1)
