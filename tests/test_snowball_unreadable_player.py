"""Gracz, ktorego historii LCU nie oddaje (08.10). Log agenta 08.10: po
~1700 graczach od 17:47 kazdy obieg konczyl sie porazka i hamowaniem do
5 min - snowball_next jest deterministyczny, a porazka LCU (decyzja 23.09)
nie oznacza gracza jako sprawdzonego, wiec ten sam puuid stal na czele
kolejki w nieskonczonosc. Porazka przesuwa go teraz za graczy bez porazek;
po SNOWBALL_MAX_FAILS czeka okno rewizyty, ale nadal nie jest sprawdzony."""
import time

from fastapi.testclient import TestClient

from app import db
from app import main as app_main

P1 = "a" * 36
P2 = "b" * 36


def test_failed_player_goes_behind_the_rest(fresh_db):
    db.snowball_add_candidates([P1, P2], 100)
    assert db.snowball_next() == [P1]
    db.snowball_fail(P1, int(time.time()))
    assert db.snowball_next() == [P2]
    assert db.snowball_next(limit=2) == [P2, P1], "odlozony, nie wyrzucony"


def test_repeated_failures_wait_for_revisit_window(fresh_db):
    db.snowball_add_candidates([P1], 100)
    now = int(time.time())
    for _ in range(db.SNOWBALL_MAX_FAILS):
        db.snowball_fail(P1, now)
    assert db.snowball_next() == []
    with db.connect() as con:
        con.execute("UPDATE snowball_seen SET failed_at=?",
                    (now - (db.SNOWBALL_REVISIT_DAYS + 1) * 86400,))
    assert db.snowball_next() == [P1]
    with db.connect() as con:
        assert con.execute("SELECT checked_at FROM snowball_seen").fetchone()[0] == 0


def test_success_clears_failures(fresh_db):
    db.snowball_add_candidates([P1, P2], 100)
    db.snowball_fail(P1, int(time.time()))
    db.snowball_mark(P1, kiwi_games=2, new_rows=10)
    with db.connect() as con:
        assert con.execute("SELECT fails FROM snowball_seen WHERE puuid=?",
                           (P1,)).fetchone()[0] == 0


def test_fail_endpoint_records_attempt(fresh_db):
    db.snowball_add_candidates([P1, P2], 100)
    with TestClient(app_main.app) as client:
        r = client.post("/api/snowball/fail", json={"puuid": P1, "status": 500})
        assert r.status_code == 200 and r.json() == {"fails": 1}
        assert client.post("/api/snowball/fail", json={"puuid": "x"}).status_code == 400
        assert client.get("/api/snowball/next").json()["puuids"] == [P2]


ZERO = "00000000-0000-0000-0000-000000000000"


def test_empty_puuid_never_enters_or_leaves_the_queue(fresh_db):
    # (08.10, log 18:50) zrzut eog zawiera zerowy puuid - LCU odpowiada
    # na niego HTTP 400 zawsze, wiec to on stal na czele kolejki
    with db.connect() as con:                  # wiersz sprzed poprawki
        con.execute("INSERT INTO snowball_seen (puuid, added_at) VALUES (?, 1)", (ZERO,))
    db.snowball_add_candidates([P1], 100)
    assert db.snowball_next(limit=5) == [P1]
    with TestClient(app_main.app) as client:
        r = client.post("/api/snowball/candidates", json={"puuids": [ZERO, P2]})
        assert r.json()["received"] == 1
