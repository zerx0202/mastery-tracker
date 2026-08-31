"""Klasy z tagow DD jako posredni poziom: referencja live (drabinka
champion -> klasa -> global) i kotwica norm. Bez tagow wszystko ma
dzialac po staremu - to gwarantuje bezpieczny rollout."""
from app import db
from tests.conftest import insert_row


def _champ(con, cid, name, key, tags=None):
    con.execute("INSERT OR REPLACE INTO champion (id, name, key, tags) "
                "VALUES (?,?,?,?)", (cid, name, key, tags))


def _graded(con, i, cid, grade, gold=18000):
    mid = f"EUW1_{1000 + i}"
    insert_row(con, "match_player", match_id=mid, duration=1200,
               game_mode="KIWI", champion_id=cid, kills=8, deaths=4,
               assists=12, cs=40, gold=gold)
    insert_row(con, "grade_observation", match_id=mid, game_id=1000 + i,
               champion_id=cid, grade=grade, observed_at=1)


def test_reference_ladder(fresh_db):
    with db.connect() as con:
        _champ(con, 1, "Lux", "Lux", "Mage,Support")
        _champ(con, 2, "Ziggs", "Ziggs", "Mage")
        _champ(con, 3, "Ornn", "Ornn", "Tank,Fighter")
        for i in range(3):
            _graded(con, i, 1, "A")            # 3 trafienia na Lux (Mage)
        _graded(con, 9, 3, "B")                # Ornn bez trafien
        con.commit()

    r = db.reference_pace("A-", "KIWI", champion_id=1)
    assert r["scope"] == "champion" and r["hit_games"] == 3

    r = db.reference_pace("A-", "KIWI", champion_id=2)     # Mage, 0 wlasnych
    assert r["scope"] == "class" and r["scope_label"] == "Mage"
    assert r["hit_games"] == 3

    r = db.reference_pace("A-", "KIWI", champion_id=3)     # Tank bez trafien
    assert r["scope"] == "global"

    r = db.reference_pace("A-", "KIWI")                     # bez championa
    assert r["scope"] == "global" and r["scope_label"] is None


def _sb_game(gid, cid, dmg):
    return {"gameId": gid, "gameMode": "KIWI", "queueId": 2400,
            "gameDuration": 1200, "gameCreation": 1700000000000,
            "participants": [{"championId": cid, "teamId": 100,
                              "stats": {"totalDamageDealtToChampions": dmg}}]}


def test_norms_class_anchor(fresh_db):
    with db.connect() as con:
        _champ(con, 11, "Syndra", "Syndra", "Mage")
        _champ(con, 12, "Anivia", "Anivia", "Mage")
        _champ(con, 13, "Bez Tagu", "BezTagu", None)
        con.commit()
    # klasa Mage: 6 gier wysokiego dmg + 1 gra niska; bez tagu: 1 gra
    db.snowball_ingest("p1", [_sb_game(100 + i, 11, 60000) for i in range(6)])
    db.snowball_ingest("p2", [_sb_game(200, 12, 10000), _sb_game(201, 13, 10000)])

    d = db.champion_norms("totalDamageDealtToChampions", mode="KIWI")
    a, g = d["champions"][12], d["champions"][13]
    assert a["class"] == "Mage" and a["anchor"] == "klasa"
    assert g["class"] is None and g["anchor"] == "global"
    # kotwica klasowa (wysokie wartosci Syndry) ciagnie Anivie wyzej,
    # niz ciagnalby ja global zanizony przez championa bez tagu
    assert a["mean"] > g["mean"]
