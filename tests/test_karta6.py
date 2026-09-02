"""Karta 6 (zgoda czlowieka 2.09 po nowej przeslance): slownik augmentow
Mayhema z kiwi.bin.json - id z naszych eog dostaja nazwy i rarity.
Walidacja id na eksporcie wykonana wczesniej (C6: pokrycie 57/57).
Granica bez zmian: WYLACZNIE etykiety/wyswietlanie, nigdy cecha modelu."""
import json

from fastapi.testclient import TestClient

from app import augments, db
from app.main import app
from tests.conftest import insert_row

# Miniatura kiwi.bin.json: dwa augmenty (jeden bez rarity - w zywym pliku
# 62/222 wpisow go nie ma), smiec bez __type i obiekt innego typu.
BIN = json.dumps({
    "a": {"__type": "AugmentData", "AugmentPlatformId": 1238,
          "AugmentNameId": "ARAM_TransmutePrismatic", "rarity": 2},
    "b": {"__type": "AugmentData", "AugmentPlatformId": 2111,
          "AugmentNameId": "Bonk"},
    "c": {"__type": "SpellObject", "AugmentPlatformId": 999},
    "d": {"nie": "augment"},
})


def test_humanize_names():
    assert augments.humanize("ARAM_TransmutePrismatic") == "Transmute Prismatic"
    assert augments.humanize("WarlockJuicebox") == "Warlock Juicebox"
    assert augments.humanize("ARAM_StatsOnStats") == "Stats On Stats"
    assert augments.humanize("Upgrade_DeathsDance") == "Upgrade Deaths Dance"
    assert augments.humanize("Bonk") == "Bonk"


def test_parse_and_store_roundtrip(fresh_db, monkeypatch):
    monkeypatch.setattr(augments, "MIN_AUGMENTS", 2)
    out = augments.store_augments(BIN, patch="26.17", ts=1700000000)
    assert out["stored"] is True and out["count"] == 2
    book = db.get_json_setting("augment_book")
    assert book["patch"] == "26.17"
    assert book["augments"]["1238"] == {
        "name": "Transmute Prismatic", "name_id": "ARAM_TransmutePrismatic",
        "rarity": 2}
    assert book["augments"]["2111"]["rarity"] == 0   # brak pola = Silver


def test_store_refuses_suspiciously_small_parse(fresh_db):
    # zywy plik ma 222 wpisy; miniatura ponizej progu = przebudowa binu,
    # stare dane maja zostac (wzorzec bramki z balance.py)
    augments.store_augments(BIN, patch="26.16", ts=1)
    out = augments.store_augments(BIN, patch="26.17", ts=2)
    assert out["stored"] is False
    assert db.get_json_setting("augment_book") is None
    kinds = [e["kind"] for e in db.recent_events(5)]
    assert "augments_parse_failed" in kinds


def test_names_for_maps_known_and_unknown(fresh_db, monkeypatch):
    monkeypatch.setattr(augments, "MIN_AUGMENTS", 2)
    augments.store_augments(BIN, patch="26.17", ts=1)
    out = augments.names_for([1238, 7007])
    assert out[0] == {"id": 1238, "name": "Transmute Prismatic", "rarity": 2}
    assert out[1] == {"id": 7007, "name": None, "rarity": None}


def test_augments_endpoint_and_history_labels(fresh_db, monkeypatch):
    monkeypatch.setattr(augments, "MIN_AUGMENTS", 2)
    augments.store_augments(BIN, patch="26.17", ts=1)
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_1", champion_id=45,
                   duration=1200, game_mode="KIWI")
        insert_row(con, "grade_observation", match_id="EUW1_1", game_id=1,
                   champion_id=45, grade="S", observed_at=5)
        insert_row(con, "eog_raw", match_id="EUW1_1", game_id=1,
                   augments=json.dumps([1238, 2111]), payload=b"x",
                   captured_at=5)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/augments").json()["count"] == 2
    g = client.get("/api/grades/history?mode=KIWI").json()["grades"][0]
    assert [a["name"] for a in g["augments"]] == ["Transmute Prismatic", "Bonk"]


def test_augment_stats_descriptive_counts(fresh_db, monkeypatch):
    """Zestawienie OPISOWE (per-augment n jednocyfrowe - zadnych wnioskow
    ilosciowych): gry, >=A-, dokladne >=S-, wygrane."""
    monkeypatch.setattr(augments, "MIN_AUGMENTS", 2)
    augments.store_augments(BIN, patch="26.17", ts=1)
    with db.connect() as con:
        for gid, grade, win, augs in ((1, "S", 1, [1238, 2111]),
                                      (2, "B", 0, [1238]),
                                      (3, ">=A-", 1, [7007])):
            insert_row(con, "match_player", match_id=f"EUW1_{gid}",
                       champion_id=45, duration=1200, game_mode="KIWI",
                       win=win)
            insert_row(con, "grade_observation", match_id=f"EUW1_{gid}",
                       game_id=gid, champion_id=45, grade=grade, observed_at=gid)
            insert_row(con, "eog_raw", match_id=f"EUW1_{gid}", game_id=gid,
                       augments=json.dumps(augs), payload=b"x", captured_at=gid)
    client = TestClient(app, raise_server_exceptions=False)
    rows = {r["id"]: r for r in client.get("/api/augments/stats").json()["augments"]}
    a = rows[1238]
    assert a["name"] == "Transmute Prismatic"
    assert a["games"] == 2 and a["a_minus"] == 1 and a["s_minus"] == 1
    assert a["wins"] == 1
    b = rows[7007]           # id spoza slownika tez widoczne (name None)
    assert b["name"] is None and b["games"] == 1 and b["a_minus"] == 1
