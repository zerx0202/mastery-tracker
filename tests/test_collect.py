"""Zbieranie danych ulotnych (Fala 1): itemy z bloku eog + log eventdata."""
import json

from app import db


def test_flatten_eog_items(fresh_db):
    block = {
        "teams": [{
            "teamId": 100,
            "players": [{
                "championId": 45,
                "isLocalPlayer": True,
                "stats": {"kills": 5, "CHAMPIONS_KILLED": 5},
                "items": [3089, 3020, {"itemId": 3157}, 0, None],
            }],
        }],
    }
    db.flatten_eog_stats(block, "EUW1_1")
    with db.connect() as con:
        keys = {r["stat_key"]: r["stat_value"] for r in con.execute(
            "SELECT stat_key, stat_value FROM player_stat WHERE match_id='EUW1_1'")}
    assert keys["item0"] == 3089.0
    assert keys["item1"] == 3020.0
    assert keys["item2"] == 3157.0          # wariant slownikowy
    assert "item3" not in keys              # zero/None = pusty slot
    assert "CHAMPIONS_KILLED" not in keys   # stara zasada bez zmian


def test_save_live_events(fresh_db):
    events = [{"EventName": "ChampionKill", "EventTime": 61.2},
              {"EventName": "TurretKilled", "EventTime": 200.0}]
    db.save_live_events(45, events)
    with db.connect() as con:
        row = con.execute("SELECT champion_id, events FROM live_event_log").fetchone()
    assert row["champion_id"] == 45
    assert len(json.loads(row["events"])) == 2
    with db.connect() as con:
        kinds = [r["kind"] for r in con.execute("SELECT kind FROM event_log")]
    assert "eventdata" in kinds
