"""Popularnosc championow w snowballu (pop_tier w rankingu) liczy sie
z calej najwiekszej tabeli bazy - na Macu (virtiofs) ~3 s z 3,7 s /targets,
a front odswieza ranking co 4 s. Wynik zmienia sie tylko, gdy snowball
dopisze gry, wiec /targets bierze go z pamieci (TTL), osobno per baza."""
import time

from app import db
from app import main as app_main
from tests.conftest import insert_row


def _seed_sb(n_matches, cid=45, start=0):
    with db.connect() as con:
        for i in range(start, start + n_matches):
            insert_row(con, "player_stat", match_id=f"SB_{cid}_{i}",
                       participant_no=1, champion_id=cid,
                       stat_key="kills", stat_value=1)


def _counting(monkeypatch):
    calls = []
    real = db.champion_sb_popularity

    def spy():
        calls.append(1)
        return real()
    monkeypatch.setattr(db, "champion_sb_popularity", spy)
    return calls


def test_popularity_is_reused_between_ranking_requests(fresh_db, monkeypatch):
    _seed_sb(2)
    calls = _counting(monkeypatch)
    app_main._SB_POP_CACHE.clear()
    assert app_main.sb_popularity() == {45: 2}
    _seed_sb(1, start=2)
    # w oknie TTL ta sama odpowiedz bez ponownego skanu
    assert app_main.sb_popularity() == {45: 2}
    assert len(calls) == 1


def test_popularity_refreshes_after_ttl(fresh_db, monkeypatch):
    _seed_sb(2)
    calls = _counting(monkeypatch)
    app_main._SB_POP_CACHE.clear()
    app_main.sb_popularity()
    _seed_sb(1, start=2)
    later = time.time() + app_main.SB_POP_TTL + 1
    monkeypatch.setattr(app_main.time, "time", lambda: later)
    assert app_main.sb_popularity() == {45: 3}
    assert len(calls) == 2


def test_popularity_cache_is_per_database(tmp_path, monkeypatch):
    # swiat testu i przywrocona kopia nie moga dostac cudzego wyniku
    app_main._SB_POP_CACHE.clear()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    db.migrate()
    _seed_sb(2)
    assert app_main.sb_popularity() == {45: 2}
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "b.db")
    db.migrate()
    _seed_sb(1, cid=99)
    assert app_main.sb_popularity() == {99: 1}
