from app import db
from tests.conftest import insert_row


def test_normalize_champion_id_offset_only_for_known_modes():
    assert db.normalize_champion_id(60029, "JADE") == 29
    assert db.normalize_champion_id(60029, "KIWI") == 60029
    assert db.normalize_champion_id(60029, None) == 60029
    assert db.normalize_champion_id(950, "KIWI") == 950
    assert db.normalize_champion_id(None, "JADE") is None


def test_grade_rank_is_monotonic():
    assert db.GRADE_RANK["S+"] > db.GRADE_RANK["S-"] > db.GRADE_RANK["A-"] \
        > db.GRADE_RANK["B+"] > db.GRADE_RANK["C"] > db.GRADE_RANK["D-"]


def test_champion_norms_shrinks_single_observation_to_global(fresh_db):
    with db.connect() as con:
        # 10 graczy w jednym meczu: dziewieciu po 600 zlota/min, jeden 1200
        insert_row(con, "match_player", match_id="M1", duration=600,
                   game_mode="KIWI", champion_id=1)
        for cid in range(1, 11):
            per_min = 1200 if cid == 10 else 600
            insert_row(con, "player_stat", match_id="M1", champion_id=cid,
                       participant_no=cid, stat_key="goldEarned",
                       stat_value=per_min * 10)
        con.commit()

    d = db.champion_norms("goldEarned", mode="KIWI")
    assert d["global"] is not None
    outlier = d["champions"][10]
    # jedna obserwacja nie ma prawa ustawic sredniej championa na 1200 -
    # przy NORM_SHRINK=8 wynik musi lezec blisko sredniej globalnej
    assert outlier["mean_raw"] == 1200
    assert outlier["mean"] < 800
    assert 0 < outlier["confidence"] < 0.2


def test_norm_z_direction(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="M1", duration=600,
                   game_mode="KIWI", champion_id=1)
        for cid in range(1, 11):
            insert_row(con, "player_stat", match_id="M1", champion_id=cid,
                       participant_no=cid, stat_key="goldEarned",
                       stat_value=6000)
        con.commit()
    hi = db.norm_z(1, "goldEarned", 900, mode="KIWI", cache={})
    lo = db.norm_z(1, "goldEarned", 300, mode="KIWI", cache={})
    assert hi["z"] > 0 > lo["z"]
