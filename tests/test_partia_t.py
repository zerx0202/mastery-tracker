"""Partia T (9.09): bramki zmeczenie 80, eventdata 67/50, cechy 100 i duza
100 otwarte naraz. Narzedzia: timing_analysis dostal sys.path (padal na
`from app.model`) i tozsamosc gracza z puuid_cache (puste `me` = zero
smierci = falszywy "brak sygnalu" i "kasacja zbierania"), plus straznik
zerowych smierci; nowy gotowiec tools/big_review.py (46/kNN/CUSUM/44b)
z prerejestrowanym protokolem w docstringu."""
import json

from app import db
from tests.conftest import insert_row
from tests.test_tools_e import _load


def _seed_timing(games, victim):
    with db.connect() as con:
        insert_row(con, "puuid_cache", riot_id="Ja#EUW", puuid="x" * 78, fetched_at=1)
        for i in range(games):
            ts = 1_700_000_000 + i * 3600
            insert_row(con, "live_event_log", saved_at=ts, champion_id=45,
                       events=json.dumps([{"EventName": "ChampionKill", "EventTime": 400,
                                           "KillerName": "Obcy", "VictimName": victim}]))
            insert_row(con, "grade_observation", match_id=f"EUW1_{i}", game_id=i,
                       champion_id=45, grade="A" if i % 2 else "B", censored=0,
                       observed_at=ts + 60)


def test_timing_uses_game_name_from_puuid_cache(fresh_db, capsys):
    ta = _load("timing_analysis")
    _seed_timing(ta.GATE, "Ja")                      # ofiara = ja -> smierci sa
    with db.connect() as con:
        assert ta.my_game_name(con) == "Ja"
    assert ta.main(db_path=db.DB_PATH) == 0
    out = capsys.readouterr().out
    assert "gracz: Ja" in out and "WERDYKT" in out


def test_timing_refuses_verdict_when_player_never_matches(fresh_db, capsys):
    ta = _load("timing_analysis")
    _seed_timing(ta.GATE, "KtosInny")                # nazwa nie pasuje -> zero smierci
    assert ta.main(db_path=db.DB_PATH) == 3
    assert "nie rozpoznano gracza" in capsys.readouterr().out


def test_big_review_mechanics(fresh_db, capsys):
    br = _load("big_review")
    # CUSUM: trafienia zgodne z p -> stabilny; same pudla przy p=0.8 -> dryf
    assert br.cusum([(0.5, 1), (0.5, 0)] * 10)[2] is False
    smax, lim, alarm = br.cusum([(0.8, 0)] * 12)
    assert alarm is True and smax > lim
    assert br.cusum([(0.8, 0)] * 5)[2] is False       # ponizej minimum par
    z = br.zscore([1, 2, 3])
    assert abs(sum(z)) < 1e-9 and z[0] < 0 < z[2]
    assert br.main(db_path=db.DB_PATH) == 2            # bramka: 0/100 obserwacji
    assert "bramka" in capsys.readouterr().out
