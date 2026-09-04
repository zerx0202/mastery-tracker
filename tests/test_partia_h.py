"""Partia H (4.09): dopiecie zaleglych pul do gier z historii/odzysku.
link_pool_to_match wola wylacznie /eog, wiec gra, ktorej koniec agent
przegapil, zostawiala pule bez meczu, a predykcja sprzed gry wisiala bez
wyniku (kopia 4.09: Ezreal 31.08 z okna "grano bez agenta"; licznik
"pule z nieprzypisana gra" liczyl do tego Practice Tool i Classic po
dodge'u Mayhema - 9 falszywych alarmow)."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row

T0 = 1_700_000_000


def _world():
    with db.connect() as con:
        # jeden champ select = dwa wiersze puli (stan czesciowy 4 i pelny 12)
        insert_row(con, "champ_select_pool", ts=T0, champion_ids="[1,2,3,4]",
                   pool_size=4, queue_id=2400)
        insert_row(con, "champ_select_pool", ts=T0 + 10,
                   champion_ids="[1,2,3,4,5,6,7,8,9,10,11,81]", pool_size=12,
                   queue_id=2400)
        # gra misji z historii: bez /eog, wiec bez puli
        insert_row(con, "match_player", match_id="EUW1_10", champion_id=81,
                   game_mode="KIWI", queue_id=2400, duration=1454,
                   game_creation=(T0 + 90) * 1000)
        # Practice Tool godzine pozniej: inny tryb, nie ma prawa dostac puli
        insert_row(con, "match_player", match_id="EUW1_11", champion_id=266,
                   game_mode="PRACTICETOOL", queue_id=3140, duration=17,
                   game_creation=(T0 + 3600) * 1000)
        # remake KIWI dzien pozniej z wlasna pula - bez oceny, bez linkowania
        insert_row(con, "champ_select_pool", ts=T0 + 86400, champion_ids="[5]",
                   pool_size=1, queue_id=2400)
        insert_row(con, "match_player", match_id="EUW1_12", champion_id=5,
                   game_mode="KIWI", queue_id=2400, duration=200,
                   game_creation=(T0 + 86400 + 60) * 1000)


def test_link_orphan_pools_links_latest_pool_of_same_queue(fresh_db):
    _world()
    assert db.pipeline_sanity()["games_unlinked_pool"] == 1
    assert db.link_orphan_pools() == 1
    with db.connect() as con:
        rows = {r["ts"]: (r["match_id"], r["picked_id"]) for r in
                con.execute("SELECT ts, match_id, picked_id FROM champ_select_pool")}
    assert rows[T0 + 10] == ("EUW1_10", 81)        # pelny stan puli
    assert rows[T0] == (None, None)                 # stan czesciowy zostaje
    assert rows[T0 + 86400] == (None, None)         # remake bez linkowania
    assert db.pipeline_sanity()["games_unlinked_pool"] == 0
    assert db.link_orphan_pools() == 0              # idempotentne


def test_link_orphan_pools_respects_queue_and_window(fresh_db):
    with db.connect() as con:
        # pula innej kolejki (custom 3270) tuz przed gra 2400 - nie pasuje
        insert_row(con, "champ_select_pool", ts=T0, champion_ids="[14]",
                   pool_size=1, queue_id=3270)
        # pula 2400, ale 5 h przed gra - poza oknem 4 h
        insert_row(con, "champ_select_pool", ts=T0 - 18000, champion_ids="[81]",
                   pool_size=1, queue_id=2400)
        insert_row(con, "match_player", match_id="EUW1_20", champion_id=81,
                   game_mode="KIWI", queue_id=2400, duration=1200,
                   game_creation=(T0 + 60) * 1000)
    assert db.link_orphan_pools() == 0
    assert db.pipeline_sanity()["games_unlinked_pool"] == 0


def test_linked_pool_resolves_prediction_pair(fresh_db):
    """Sens backfillu: para predykcja-wynik powstaje z samego linkowania."""
    _world()
    with db.connect() as con:
        pid = con.execute("SELECT id FROM champ_select_pool WHERE ts=?",
                          (T0 + 10,)).fetchone()["id"]
        insert_row(con, "grade_observation", match_id="EUW1_10", game_id=10,
                   champion_id=81, grade="S", observed_at=T0 + 2000)
    db.save_pool_predictions(pid, [{"champion_id": 81, "next_threshold": "S-",
                                    "model_p": 0.3, "next_p": 0.2,
                                    "model_games": 5}], T0 + 10)
    resolved, pending = db.prediction_pairs()
    assert resolved == [] and pending == 1
    db.link_orphan_pools()
    resolved, pending = db.prediction_pairs()
    assert len(resolved) == 1 and resolved[0]["grade"] == "S" and pending == 0


def test_history_ingest_triggers_backfill(fresh_db, monkeypatch):
    from app import main, model
    calls = []
    monkeypatch.setattr(db, "save_lcu_game", lambda g, my=None: True)
    monkeypatch.setattr(db, "link_orphan_pools", lambda: calls.append(1) or 2)
    monkeypatch.setattr(model, "train", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "get_cached_puuid", lambda name: None)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/history/lcu", json={"games": [{"gameId": 1}]})
    assert r.json()["new"] == 1 and calls == [1]
    ev = [e for e in db.recent_events(5) if e["kind"] == "pool_link_backfill"]
    assert ev and ev[0]["detail"]


def test_migrate_runs_backfill_idempotently(fresh_db):
    _world()
    db.migrate()                                    # upgrade_link_orphan_pools
    assert db.pipeline_sanity()["games_unlinked_pool"] == 0
    db.migrate()
    assert db.link_orphan_pools() == 0
