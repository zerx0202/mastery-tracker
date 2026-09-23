"""Baner "Grano bez agenta" (23.09: wisial 13 dni po oknie z 10.09).
Ostrzezenie ma sens tylko, dopoki gry z luki siedza w oknie 20 ostatnich
gier historii klienta - pozniej zadne dzialanie ich nie odzyska listingiem,
wiec luka starsza niz 20. najnowsza gra znika z banera."""
from app import db
from tests.conftest import insert_row

HOUR = 3600
T0 = 1_790_000_000


def _snap(sid, ts, points):
    with db.connect() as con:
        insert_row(con, "snapshot", id=sid, taken_at=ts)
        insert_row(con, "mastery", snapshot_id=sid, champion_id=45, points=points)


def _games(n, start):
    with db.connect() as con:
        for i in range(n):
            insert_row(con, "match_player", match_id=f"EUW1_{start + i}",
                       game_creation=(start + i * HOUR) * 1000, duration=1200)


def test_gap_older_than_client_history_window_is_dropped(fresh_db):
    _snap(1, T0, 1000)
    _snap(2, T0 + HOUR, 1500)                 # luka: punkty rosna, brak eog
    _games(20, T0 + 10 * HOUR)                # 20 nowszych gier - luka poza oknem
    assert db.agent_activity_gaps() == []


def test_gap_inside_client_history_window_stays(fresh_db):
    _games(5, T0 - 10 * HOUR)
    _snap(1, T0, 1000)
    _snap(2, T0 + HOUR, 1500)
    gaps = db.agent_activity_gaps()
    assert len(gaps) == 1 and gaps[0]["points_delta"] == 500
