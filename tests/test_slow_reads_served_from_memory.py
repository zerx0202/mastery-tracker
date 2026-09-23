"""Wydajnosc widokow (23.09, zrzuty: Teraz LCP 3,96 s, Oceny 3,02 s, System
2,68 s; pomiar na kopii 5,7 mln wierszy): ciezkie odczyty licza sie rzadko,
ale ktos zawsze na nie czekal. Popularnosc snowballa i normy championow
oddaja STARA wartosc po TTL i przeliczaja sie w tle; status modelu nie
zlacza ocen z cala tabela statystyk; historia ocen czyta model raz, nie
3x na wiersz."""
import time

from app import db
from app import main as app_main
from tests.conftest import insert_row


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_stale_value_is_served_while_refreshing_in_background():
    store, calls = {}, []

    def compute():
        calls.append(1)
        time.sleep(0.3)
        return len(calls)
    assert db.swr_get(store, "k", 60, compute) == 1          # pierwsze: liczy
    store["k"] = (time.time() - 61, store["k"][1])            # TTL minal
    t0 = time.time()
    assert db.swr_get(store, "k", 60, compute) == 1           # stara, od razu
    assert time.time() - t0 < 0.2, "zadanie nie moze czekac na przeliczenie"
    assert db.swr_get(store, "k", 60, compute) == 1           # bez drugiego watku
    assert _wait(lambda: store["k"][1] == 2)
    assert len(calls) == 2


def test_failed_refresh_keeps_old_value():
    store = {"k": (time.time() - 61, "stara")}

    def boom():
        raise RuntimeError("baza zajeta")
    assert db.swr_get(store, "k", 60, boom) == "stara"
    time.sleep(0.2)
    assert store["k"][1] == "stara"


def test_champion_norms_are_reused_after_ttl(fresh_db, monkeypatch):
    calls = []
    real = db.champion_norms

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(db, "champion_norms", spy)
    db._NORM_CACHE.clear()
    db.norm_z(45, "totalDamageDealtToChampions", 1000, "KIWI")
    for key, (ts, val) in list(db._NORM_CACHE.items()):
        db._NORM_CACHE[key] = (ts - db._NORM_TTL - 1, val)
    t0 = time.time()
    db.norm_z(45, "totalDamageDealtToChampions", 1000, "KIWI")
    assert time.time() - t0 < 0.2
    assert _wait(lambda: len(calls) == 2)


def test_grades_history_reads_model_settings_once(fresh_db, monkeypatch):
    db.set_json_setting("grade_model", {"features": [], "models": {}})
    with db.connect() as con:
        for i in range(30):
            insert_row(con, "match_player", match_id=f"EUW1_{i}", game_mode="KIWI",
                       duration=1200, champion_id=45, kills=5, deaths=5,
                       assists=5, gold=12000, dmg_champ=20000, cs=40)
            insert_row(con, "grade_observation", match_id=f"EUW1_{i}", game_id=i,
                       champion_id=45, grade="A", observed_at=i)
    reads = []
    real = db.get_json_setting
    monkeypatch.setattr(db, "get_json_setting",
                        lambda k: reads.append(k) or real(k))
    import app.model as model
    monkeypatch.setattr(model, "get_json_setting",
                        lambda k: reads.append(k) or real(k))
    out = app_main._grades_history_sync(30, "KIWI")
    assert len(out["grades"]) == 30
    assert len(reads) <= 5, f"ustawienia czytane {len(reads)}x dla 30 wierszy"


def test_model_status_counts_without_full_join(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", duration=1200)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="A", observed_at=1)
        insert_row(con, "grade_observation", match_id="EUW1_2", game_id=2,
                   champion_id=45, grade=">=A-", censored=1, observed_at=2)
        for k in ("kills", "deaths"):
            insert_row(con, "player_stat", match_id="EUW1_1", participant_no=1,
                       champion_id=45, is_local=1, stat_key=k, stat_value=1)
        insert_row(con, "player_stat", match_id="EUW1_1", participant_no=2,
                   champion_id=12, is_local=0, stat_key="kills", stat_value=1)
    st = db.model_status()
    assert st["grades_total"] == 2 and st["grades_exact"] == 1
    assert st["grades_with_stats"] == 2          # wlasne wiersze statystyk ocenionych gier
    assert st["grades_with_full_stats"] == 1     # oceniony mecz z wlasnymi statystykami
    assert st["grades_with_match_stats"] == 1
    with db.connect() as con:
        plan = " ".join(r["detail"] for q in db.MODEL_STATUS_SQL.values()
                        for r in con.execute("EXPLAIN QUERY PLAN " + q))
    assert "SCAN p" not in plan and "SCAN player_stat" not in plan, plan


def test_startup_warms_heavy_reads(fresh_db):
    app_main._SB_POP_CACHE.clear()
    db._NORM_CACHE.clear()
    app_main.warm_caches()
    assert str(db.DB_PATH) in app_main._SB_POP_CACHE
    assert any(k[0] == str(db.DB_PATH) for k in db._NORM_CACHE)
    assert str(db.DB_PATH) in app_main._ROWCOUNT_CACHE


def test_stat_row_count_is_served_from_memory(fresh_db):
    app_main._ROWCOUNT_CACHE.clear()
    with db.connect() as con:
        insert_row(con, "player_stat", match_id="SB_1", participant_no=1,
                   champion_id=45, stat_key="kills", stat_value=1)
    assert app_main._system_health_sync()["counts"]["player_stat"] == 1
    assert str(db.DB_PATH) in app_main._ROWCOUNT_CACHE
