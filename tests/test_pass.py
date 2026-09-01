"""Pakiet przepustki: tempo grania + magazyn stanu event-hubu."""
import time

from app import db
from tests.conftest import insert_row


def test_recent_tempo(fresh_db):
    now_s = int(time.time())
    with db.connect() as con:
        # 3 gry KIWI w oknie (w tym jedna z czasem w ms), 1 stara, 1 inny tryb
        insert_row(con, "match_player", match_id="EUW1_1", duration=1200,
                   game_mode="KIWI", champion_id=1, game_creation=now_s - 3600)
        insert_row(con, "match_player", match_id="EUW1_2", duration=1200,
                   game_mode="KIWI", champion_id=1,
                   game_creation=(now_s - 7200) * 1000)
        insert_row(con, "match_player", match_id="EUW1_3", duration=1200,
                   game_mode="KIWI", champion_id=1, game_creation=now_s - 86400)
        insert_row(con, "match_player", match_id="EUW1_4", duration=1200,
                   game_mode="KIWI", champion_id=1,
                   game_creation=now_s - 30 * 86400)
        insert_row(con, "match_player", match_id="EUW1_5", duration=1200,
                   game_mode="ARAM", champion_id=1, game_creation=now_s - 3600)
        con.commit()
    assert db.recent_tempo("KIWI", days=7) == round(3 / 7, 2)
    assert db.recent_tempo(None, days=7) == round(4 / 7, 2)


def test_pass_state_roundtrip(fresh_db):
    db.set_json_setting("pass_state", {"ts": 1, "events": [
        {"event_id": "x", "name": "Mayhem Set 2", "days_left": 35.0,
         "progress": {"level": 32, "totalLevels": 32}}]})
    st = db.get_json_setting("pass_state")
    assert st["events"][0]["name"] == "Mayhem Set 2"
