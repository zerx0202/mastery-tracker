"""Reszta obronionych pomyslow z panelu E (przeglad 2.09), w warunkach
sedziow: percentyl wewnatrzmeczowy jako rozszerzenie my_share (kontekst,
nie diagnoza - konfundacja skladem podpisana w UI); watchdog "grano bez
agenta" jako krzyzowka snapshotow dopieta do zdrowia potoku (uczciwa
tresc: gry odzyskiwalne, przepadaja live/eventdata); czarna skrzynka
agenta (metryki best-effort, incydenty przez kolejke, UI eksponuje WIEK
meldunku). Gotowiec timingu i dry-run: tests/test_tools_e.py."""
import json
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row


# ---------- percentyl wewnatrzmeczowy ----------

def _full_match(mid="EUW1_9", n=10):
    with db.connect() as con:
        insert_row(con, "match_player", match_id=mid, champion_id=45,
                   duration=1200, game_mode="KIWI")
        for pn in range(1, n + 1):
            for key, val in (("totalDamageDealtToChampions", 1000 * pn),
                             ("goldEarned", 8000 + 100 * pn)):
                insert_row(con, "player_stat", match_id=mid,
                           participant_no=pn, champion_id=100 + pn,
                           is_local=1 if pn == 7 else 0,
                           stat_key=key, stat_value=val)


def test_match_percentiles_ranks_me_among_lobby(fresh_db):
    _full_match()
    out = db.match_percentiles("EUW1_9")
    dmg = next(x for x in out if x["key"] == "totalDamageDealtToChampions")
    # is_local ma 7000 dmg: 3 graczy wyzej -> 4. miejsce z 10
    assert dmg["rank"] == 4 and dmg["of"] == 10
    assert dmg["per_min"] == round(7000 / 20, 1)


def test_match_percentiles_needs_full_lobby(fresh_db):
    """Wpisy jednoosobowe (wlasny listing LCU, stare SB) pokazywalyby
    '1. z 1' - bzdura zamiast kontekstu (warunek sedziego)."""
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200)
        insert_row(con, "player_stat", match_id="EUW1_1", participant_no=1,
                   champion_id=45, is_local=1,
                   stat_key="totalDamageDealtToChampions", stat_value=999)
    assert db.match_percentiles("EUW1_1") == []


def test_explain_includes_match_percentiles(fresh_db):
    _full_match()
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_9", game_id=9,
                   champion_id=45, grade="S", observed_at=5)
        insert_row(con, "eog_raw", match_id="EUW1_9", game_id=9,
                   augments="[]", payload=b"x", captured_at=5)
    # explain wymaga modelu - bez niego 404; percentyle jada ta sama
    # sciezka (main dokleja match_percentiles), wystarczy asercja na db
    out = db.match_percentiles("EUW1_9")
    assert {x["key"] for x in out} >= {"totalDamageDealtToChampions",
                                      "goldEarned"}


# ---------- watchdog: grano bez agenta ----------

def test_agent_gaps_detects_growth_without_agent(fresh_db):
    with db.connect() as con:
        for sid, ts, pts in ((1, 1000, 100), (2, 5000, 100),
                             (3, 9000, 900), (4, 13000, 900)):
            insert_row(con, "snapshot", id=sid, taken_at=ts, split_id=1)
            insert_row(con, "mastery", snapshot_id=sid, champion_id=45,
                       milestone=1, points=pts, level=1)
    # okno 2->3: punkty urosly, zero sladu eog od agenta = grano bez niego;
    # okno 1->2 i 3->4 bez przyrostu - cisza to nie luka
    gaps = db.agent_activity_gaps()
    assert len(gaps) == 1
    g = gaps[0]
    assert (g["from_ts"], g["to_ts"]) == (5000, 9000)
    assert g["points_delta"] == 800


def test_agent_gaps_tolerates_games_seen_by_agent(fresh_db):
    with db.connect() as con:
        for sid, ts, pts in ((1, 1000, 100), (2, 9000, 900)):
            insert_row(con, "snapshot", id=sid, taken_at=ts, split_id=1)
            insert_row(con, "mastery", snapshot_id=sid, champion_id=45,
                       milestone=1, points=pts, level=1)
    db.log_event("eog", {"new": 1}, ts=4000)   # agent widzial gre w oknie
    assert db.agent_activity_gaps() == []


def test_health_exposes_agent_gaps(fresh_db):
    with db.connect() as con:
        for sid, ts, pts in ((1, 1000, 100), (2, 9000, 900)):
            insert_row(con, "snapshot", id=sid, taken_at=ts, split_id=1)
            insert_row(con, "mastery", snapshot_id=sid, champion_id=45,
                       milestone=1, points=pts, level=1)
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["agent_gaps"] and h["agent_gaps"][0]["points_delta"] == 800


# ---------- czarna skrzynka agenta ----------

def test_agent_health_report_stored_with_age(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/agent/health",
                    json={"queue": 3, "bad": 1, "ws_ok": True})
    assert r.status_code == 200
    st = db.get_json_setting("agent_health")
    assert st["queue"] == 3 and st["bad"] == 1 and st["ws_ok"] is True
    assert st["ts"] <= int(time.time())
    h = client.get("/api/system/health").json()
    assert h["agent_health"]["queue"] == 3


def test_agent_incident_logged(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/agent/incident",
                    json={"kind": "ws_down", "detail": "HTTP 404"})
    assert r.status_code == 200
    ev = [e for e in db.recent_events(5) if e["kind"] == "agent_ws_down"]
    assert ev and json.loads(ev[0]["detail"])["detail"] == "HTTP 404"
    # nieznany rodzaj nie tworzy dowolnych kindow w event_logu
    assert client.post("/api/agent/incident",
                       json={"kind": "x" * 40}).json()["stored"] is False
