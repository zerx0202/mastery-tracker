"""Rejestr snowballa: dedup graczy i okno czasowe - gracz spotkany ponownie
nie jest odpytywany czesciej niz raz na tydzien."""
import time

from app import db

P1 = "0" * 36
P2 = "1" * 36


def test_candidates_dedup(fresh_db):
    assert db.snowball_add_candidates([P1, P2], 100) == 2
    assert db.snowball_add_candidates([P1], 200) == 2  # znany, bez zmian


def test_next_respects_revisit_window(fresh_db):
    db.snowball_add_candidates([P1], 100)
    assert db.snowball_next() == [P1]          # nigdy nie sprawdzany
    db.snowball_mark(P1, kiwi_games=3, new_rows=50)
    assert db.snowball_next() == []            # swiezo sprawdzony - czeka
    with db.connect() as con:                  # symulacja uplywu 8 dni
        con.execute("UPDATE snowball_seen SET checked_at=?",
                    (int(time.time()) - 8 * 86400,))
    assert db.snowball_next() == [P1]          # okno minelo - znowu w kolejce
