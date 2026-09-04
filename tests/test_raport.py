"""Realizacja raportu ANALIZA.md, partia backendowa (2.09): grade_raw (P3),
liczniki bramek (P4), sanity potoku (P8), priorytet snowballa (P7),
meldunek backupu (P5), sonda augmentow (P9)."""
import json
import time
import zlib

from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.conftest import insert_row


# ---------- P3: grade_raw ----------

def test_grade_raw_roundtrip_via_endpoint(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    updates = [{"gameId": 500, "grade": "A-", "championId": 45,
                "polePrzyszlosci": {"x": 1}},
               {"gameId": 500, "grade": None, "championId": 45}]
    r = client.post("/api/grade", json={"updates": updates})
    assert r.status_code == 200 and not r.json()["errors"]
    raw = db.load_grade_raw("EUW1_500")
    # CALY surowiec, razem z polami, ktorych save_grade nie zna
    assert raw == updates
    with db.connect() as con:
        blob = con.execute(
            "SELECT payload FROM grade_raw WHERE match_id='EUW1_500'"
        ).fetchone()["payload"]
    assert json.loads(zlib.decompress(blob)) == updates


def test_grade_raw_skips_entries_without_gameid(fresh_db):
    assert db.save_grade_raw([{"grade": "S"}, "smiec", None], "euw1",
                             1700000000) == 0
    with db.connect() as con:
        assert con.execute(
            "SELECT COUNT(*) c FROM grade_raw").fetchone()["c"] == 0


# ---------- P4: bramki danych ----------

def test_data_gates_counts(fresh_db):
    with db.connect() as con:
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="S-", censored=0, observed_at=1)
        insert_row(con, "grade_observation", match_id="EUW1_2", game_id=2,
                   champion_id=45, grade="B+", censored=0, observed_at=2)
        insert_row(con, "grade_observation", match_id="EUW1_3", game_id=3,
                   champion_id=45, grade=">=A-", censored=1, observed_at=3)
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200)
    db.save_live_events(45, [{"EventName": "ChampionKill"}])
    gates = {g["key"]: g for g in db.data_gates()}
    assert gates["s_minus"]["have"] == 1          # tylko dokladne S-
    assert gates["fatigue"]["have"] == 2          # dokladne, bez ">="
    assert gates["eventdata"]["have"] == 1
    assert gates["class_feats"]["have"] == 1      # ocena z meczem
    assert gates["big_review"]["need"] == 100


# ---------- P8: sanity potoku ----------

def test_pipeline_sanity_counts(fresh_db):
    now = int(time.time())
    with db.connect() as con:
        # ocena z meczem (zdrowa) + ocena-sierota
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200)
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="A-", observed_at=1)
        insert_row(con, "grade_observation", match_id="EUW1_66", game_id=66,
                   champion_id=45, grade="A-", observed_at=2)
        # eog bez tozsamosci
        insert_row(con, "eog_raw", match_id="EUW1_1", game_id=1,
                   payload=b"x", captured_at=now)
        # pula stara i swieza, obie bez meczu
        insert_row(con, "champ_select_pool", ts=now - 90000,
                   champion_ids="[1]", pool_size=1)
        insert_row(con, "champ_select_pool", ts=now - 100,
                   champion_ids="[2]", pool_size=1)
    p = db.pipeline_sanity()
    # missing_games: ocena-sierota EUW1_66 to zarazem znana gra bez statystyk;
    # eog_bez_oceny=0, bo jedyny eog (EUW1_1) ma swoja ocene
    assert p == {"orphan_grades": 1, "eog_no_participants": 1,
                 "stale_pools": 1, "games_unlinked_pool": 0,
                 "missing_games": 1, "eog_bez_oceny": 0,
                 "games_without_grade": 0, "timeline_missing": 0}


# ---------- P7: priorytet snowballa ----------

def test_snowball_priority_by_shared_games(fresh_db):
    db.snowball_add_candidates(["a" * 36, "b" * 36, "c" * 36], 1700000000)
    with db.connect() as con:
        # gracz "b" widziany w dwoch meczach, "c" w jednym, "a" w zadnym
        for mid, pu in (("EUW1_1", "b"), ("EUW1_2", "b"), ("EUW1_1", "c")):
            insert_row(con, "match_participant", match_id=mid,
                       participant_no=2 if pu == "b" else 3, puuid=pu * 36)
    assert db.snowball_next(3) == ["b" * 36, "c" * 36, "a" * 36]


# ---------- P5: meldunek backupu ----------

def test_backup_report_endpoint(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/backup/report", json={"ok": False, "note": "BLAD: kontener"})
    assert r.status_code == 200
    st = db.get_json_setting("last_backup")
    assert st["ok"] is False and "kontener" in st["note"]
    assert any(e["kind"] == "backup_report" for e in db.recent_events(5))
    health = client.get("/api/system/health").json()
    assert health["last_backup"]["ok"] is False
    assert {g["key"] for g in health["gates"]} >= {"s_minus", "brier"}


# ---------- P9: sonda augmentow (czysta czesc) ----------

def test_augment_probe_coverage():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "augment_probe",
        Path(__file__).resolve().parents[1] / "tools" / "augment_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    export = {"eog_raw": [{"augments": "[101, 999]"}, {"augments": "[101]"},
                          {"augments": None}]}
    arena = {"augments": [{"id": 101, "name": "Cerberus", "rarity": 2}]}
    ids, hit, miss, by_id = probe.coverage(export, arena)
    assert ids == {101, 999}
    assert hit == [101] and miss == [999]
    assert by_id[101]["name"] == "Cerberus"
