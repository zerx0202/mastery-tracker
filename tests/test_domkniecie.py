"""Domkniecie backlogu: recap splitu (16)."""
from app import db
from tests.conftest import insert_row


def test_split_recap(fresh_db):
    with db.connect() as con:
        con.execute("INSERT INTO split (started_at, detected_at, note) "
                    "VALUES (1700000000, 1700000000, 'test')")
        for i, (win, dur) in enumerate([(1, 1200), (0, 900), (1, 1500)]):
            insert_row(con, "match_player", match_id=f"EUW1_{i}",
                       duration=dur, game_mode="KIWI", champion_id=45 + i,
                       win=win, game_creation=1700000100 + i)
        # gra sprzed splitu - nie liczy sie
        insert_row(con, "match_player", match_id="EUW1_9", duration=1200,
                   game_mode="KIWI", champion_id=1, win=1,
                   game_creation=1600000000)
        insert_row(con, "grade_observation", match_id="EUW1_0", game_id=1,
                   champion_id=45, grade="S-", observed_at=1700000200)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=2,
                   champion_id=46, grade="A", observed_at=1700000300)
        insert_row(con, "grade_observation", match_id="EUW1_2", game_id=3,
                   champion_id=47, grade=">=A-", observed_at=1700000400)
        con.commit()
    rc = db.split_recap("KIWI")
    assert rc["games"] == 3 and rc["wins"] == 2
    assert rc["hours"] == 1.0
    assert rc["unique_champions"] == 3
    assert rc["s_count"] == 1 and rc["a_count"] == 1
    assert rc["grades"] == {"S-": 1, "A": 1}      # cenzurowane poza recapem
