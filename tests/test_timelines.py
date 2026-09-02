"""Akwizycja timelines wlasnych gier KIWI przez LCU (pomysl E, sonda C3:
/lol-match-history/v1/game-timelines/{id} oddaje pelne frames dla kolejki
2400, takze dla gier spoza okna 20). WYLACZNIE surowiec wzorcem eog_raw -
zadnej analizy: pierwsze spojrzenie przy bramce eventdata/50 gier, cechy
tempa za bramka 60-100 obs. Strona agenta: tests/test_agent.py."""
import json
import zlib

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row

FRAMES = {"frames": [
    {"timestamp": 0, "events": [],
     "participantFrames": {"1": {"totalGold": 1400, "xp": 660}}},
    {"timestamp": 60000, "events": [{"type": "CHAMPION_KILL"}],
     "participantFrames": {"1": {"totalGold": 2000, "xp": 1200}}},
]}


def test_save_and_load_timeline_roundtrip(fresh_db):
    assert db.save_timeline("EUW1_9", 9, FRAMES, 1700000000) is True
    assert db.save_timeline("EUW1_9", 9, FRAMES, 1700000001) is False  # dedup
    assert db.load_timeline("EUW1_9") == FRAMES
    with db.connect() as con:
        row = con.execute("SELECT * FROM match_timeline").fetchone()
    assert row["frames"] == 2
    assert json.loads(zlib.decompress(row["payload"])) == FRAMES


def test_missing_timelines_scope_and_order(fresh_db):
    with db.connect() as con:
        # dwie gry misji bez timeline, jedna z timeline, custom i inny tryb
        for mid, mode, created in (("EUW1_1", "KIWI", 100),
                                   ("EUW1_2", "KIWI", 200),
                                   ("EUW1_3", "KIWI", 300),
                                   ("EUW1_4", "KIWI_CUSTOM", 400),
                                   ("EUW1_5", "CLASSIC", 500)):
            insert_row(con, "match_player", match_id=mid, champion_id=45,
                       duration=1200, game_mode=mode, game_creation=created)
    db.save_timeline("EUW1_3", 3, FRAMES, 1)
    # najnowsze najpierw; customy i inne tryby poza zakresem misji
    assert db.missing_timelines(10) == [2, 1]
    assert db.missing_timelines(1) == [2]


def test_timeline_endpoint_stores_and_logs(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_9", champion_id=45,
                   duration=1200, game_mode="KIWI")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/timeline", json={"game_id": 9, "timeline": FRAMES})
    assert r.status_code == 200
    assert r.json() == {"stored": True, "new": True, "frames": 2}
    assert db.load_timeline("EUW1_9") == FRAMES
    assert any(e["kind"] == "timeline" for e in db.recent_events(5))
    assert db.missing_timelines(10) == []


def test_timeline_endpoint_rejects_empty_frames(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/timeline", json={"game_id": 9,
                                           "timeline": {"frames": []}})
    assert r.status_code == 200 and r.json()["stored"] is False
    r = client.post("/api/timeline", json={"game_id": 9})
    assert r.status_code == 200 and r.json()["stored"] is False


def test_timeline_endpoint_honest_500_on_db_error(fresh_db, monkeypatch):
    """Jak /grade i /eog po partii A: awaria zapisu nie moze wygladac jak
    sukces - agent liczy niepowodzenie i wraca po gre pozniej (timeline
    jest odzyskiwalny po ID, wiec bez kolejki dyskowej)."""
    def boom(*a, **k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(db, "save_timeline", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/timeline", json={"game_id": 9, "timeline": FRAMES})
    assert r.status_code == 500


def test_pipeline_and_health_expose_timeline_counts(fresh_db):
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200, game_mode="KIWI", game_creation=1)
    p = db.pipeline_sanity()
    assert p["timeline_missing"] == 1
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["counts"]["match_timeline"] == 0
