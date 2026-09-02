"""Narzedzia z panelu E: gotowiec analizy timingu (protokol prerejestrowany
w skrypcie, odpalenie dopiero przy bramce 50 gier) i dry-run
odtwarzalnosci (surowce grade_raw/eog_raw odtwarzaja pochodne)."""
import importlib.util
import json
from pathlib import Path

from app import db
from tests.conftest import insert_row

MY = "99999999-9999-9999-9999-999999999999"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- gotowiec timingu ----------

def test_timing_profile_from_live_events():
    ta = _load("timing_analysis")
    events = [
        {"EventName": "GameStart", "EventTime": 0},
        {"EventName": "ChampionKill", "EventTime": 120,
         "KillerName": "Ja#EUW", "VictimName": "Obcy1"},
        {"EventName": "ChampionKill", "EventTime": 200,
         "KillerName": "Obcy2", "VictimName": "Ja#EUW"},
        {"EventName": "ChampionKill", "EventTime": 700,
         "KillerName": "Obcy2", "VictimName": "Ja#EUW",
         "Assisters": []},
        {"EventName": "TurretKilled", "EventTime": 400},
    ]
    p = ta.game_profile(events, me="Ja#EUW")
    assert p["deaths_total"] == 2
    assert p["deaths_0_5"] == 1 and p["deaths_5_10"] == 0
    assert p["first_death_s"] == 200
    assert p["kills_early_share"] == 1.0     # jedyny kill przed 10. minuta


def test_timing_analysis_refuses_below_gate(fresh_db, capsys):
    ta = _load("timing_analysis")
    # 1 gra w logu << bramka 50 - protokol zabrania patrzec na wynik
    with db.connect() as con:
        insert_row(con, "live_event_log", saved_at=1, champion_id=45,
                   events=json.dumps([{"EventName": "GameStart",
                                       "EventTime": 0}]))
    rc = ta.main(db_path=db.DB_PATH, force=False)
    assert rc == 2
    assert "bramka" in capsys.readouterr().out.lower()


# ---------- dry-run odtwarzalnosci ----------

def test_dry_run_rebuild_reports_zero_mismatches(fresh_db, tmp_path):
    """Pochodne zapisane produkcyjnymi sciezkami musza sie odtworzyc
    z samych surowcow - to jest dowod, ze grade_raw/eog_raw to polisa,
    a nie intencja (2 historyczne ciche bugi ekstrakcji)."""
    ts = 1700000000
    block = {"gameId": 9, "gameMode": "KIWI",
             "teams": [{"teamId": 100, "players": [
                 {"isLocalPlayer": True, "championId": 45, "puuid": MY,
                  "stats": {"WIN": 1, "goldEarned": 9000,
                            "playerAugment1": 1238}}]}]}
    db.save_grade_raw([{"gameId": 9, "grade": "S", "championId": 45,
                        "score": 700}], "euw1", ts)
    db.save_grade({"gameId": 9, "grade": "S", "championId": 45,
                   "score": 700}, "euw1", ts)
    db.save_eog(block, "euw1", ts)
    db.flatten_eog_stats(block, "EUW1_9")
    db.save_match_participants(block, "EUW1_9")

    dr = _load("dry_run_rebuild")
    report = dr.rebuild_and_compare(str(db.DB_PATH), str(tmp_path / "tmp.db"))
    assert report["grade"]["compared"] == 1 and report["grade"]["mismatch"] == 0
    assert report["eog"]["compared"] == 1 and report["eog"]["mismatch"] == 0
    assert report["player_stat"]["mismatch"] == 0
    assert report["participants"]["mismatch"] == 0


def test_dry_run_rebuild_catches_extraction_drift(fresh_db, tmp_path,
                                                  monkeypatch):
    """Zepsuta ekstrakcja = rozjazd w raporcie, nie cichy sukces."""
    ts = 1700000000
    db.save_grade_raw([{"gameId": 9, "grade": "S", "championId": 45}],
                      "euw1", ts)
    db.save_grade({"gameId": 9, "grade": "S", "championId": 45}, "euw1", ts)
    # symulacja przyszlego buga: pochodna w kopii zostala zapisana inaczej
    with db.connect() as con:
        con.execute("UPDATE grade_observation SET grade='A' "
                    "WHERE match_id='EUW1_9'")
    dr = _load("dry_run_rebuild")
    report = dr.rebuild_and_compare(str(db.DB_PATH), str(tmp_path / "tmp.db"))
    assert report["grade"]["mismatch"] == 1
