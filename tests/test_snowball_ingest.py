from app import db
from tests.conftest import insert_row


def G(gid, mode="KIWI", qid=2400, dur=900, cid=51, dmg=30000):
    return {"gameId": gid, "gameMode": mode, "queueId": qid,
            "gameDuration": dur, "gameCreation": 1700000000000,
            "participants": [{"championId": cid, "teamId": 100,
                              "stats": {"totalDamageDealtToChampions": dmg,
                                        "goldEarned": 12000, "win": True}}]}


def test_ingest_filters_and_dedups(fresh_db):
    games = [G(1), G(2, mode="ARAM", qid=450), G(3, dur=120)]
    kiwi, rows = db.snowball_ingest("p1", games)
    assert kiwi == 2 and rows == 3          # gra 2 odpada (tryb), 3 (remake)
    kiwi2, rows2 = db.snowball_ingest("p1", games)
    assert rows2 == 0                        # dedup po game_id


def test_ingest_skips_own_games(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_777", duration=900,
                   game_mode="KIWI", champion_id=1)
        con.commit()
    kiwi, rows = db.snowball_ingest("p1", [G(777)])
    assert kiwi == 1 and rows == 0           # moja gra - pelne dane juz sa


def test_norms_see_snowball_data(fresh_db):
    db.snowball_ingest("p1", [G(10, cid=99), G(11, cid=99)])
    d = db.champion_norms("totalDamageDealtToChampions", mode="KIWI")
    assert d["global"] is not None and 99 in d["champions"]


def test_ingest_suffix_is_not_own(fresh_db):
    """gid bedacy sufiksem CUDZEGO id (777 vs EUW1_9777) nie moze byc
    uznany za wlasna gre - filtr wymaga separatora _ przed gid."""
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_9777", duration=900,
                   game_mode="KIWI", champion_id=1)
        con.commit()
    kiwi, rows = db.snowball_ingest("p1", [G(777)])
    assert kiwi == 1 and rows == 3           # zaingestowana, nie odfiltrowana
