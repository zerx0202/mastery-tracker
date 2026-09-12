"""Partia V (12.09): notatki o graczach - "kto to byl" wpisane reka,
widoczne w champ selekcie (zeton), w grze (panel live), w wierszu oceny
i w tabeli Laboratorium. Osobna tabela player_note pod puuid klienta
(36 znakow): kolumna w player_name ginelaby przy kazdym ekranie koncowym
(save_player_names = INSERT OR REPLACE). Pusta notatka kasuje wiersz."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_partia_m import _game

MY, A, B, C = "m" * 36, "a" * 36, "b" * 36, "c" * 36
NOW = 1_700_000_000


def test_set_get_and_delete_note(fresh_db):
    assert db.set_player_note(A, "  flamer, muted  ", ts=NOW) == "flamer, muted"
    assert db.get_player_notes([A, B]) == {A: "flamer, muted"}
    assert db.set_player_note(A, "dobry adc", ts=NOW + 1) == "dobry adc"
    with db.connect() as con:
        assert con.execute("SELECT updated_at FROM player_note WHERE puuid=?",
                           (A,)).fetchone()[0] == NOW + 1
    assert db.set_player_note(A, "   ") is None
    assert db.get_player_notes([A]) == {} and db.get_player_notes(None) == {}


def test_note_folded_into_summary_recurring_and_lonely_player(fresh_db):
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 100, 238)])
    _game("EUW1_2", 2, 0, NOW + 3600, [(MY, "Ja#1", 200, 45), (A, "Zed#EUW", 100, 238)])
    db.set_player_note(A, "x")
    s = db.players_summary([A, B], MY)
    assert s[A]["note"] == "x" and B not in s
    # gracz z notatka bez historii i bez nazwy tez wraca - zeton w champ
    # selekcie ma pokazac notatke, zanim eog dopisze nazwe
    db.set_player_note(B, "nowy bez gier")
    s = db.players_summary([B], MY)
    assert s[B]["games"] == 0 and s[B]["note"] == "nowy bez gier" and s[B]["name"] is None
    assert db.recurring_players(MY)[0]["note"] == "x"
    # bez mojego puuid historii nie zgadujemy, ale notatka wraca (zeton w champ
    # selekcie zanim eog nauczy my_lcu_puuid); B bez notatki nadal nie wraca
    lonely = db.players_summary([A, B, C], None)
    assert lonely[A]["note"] == "x" and lonely[A]["games"] == 0
    assert lonely[B]["note"] == "nowy bez gier" and C not in lonely


def test_note_survives_name_replace(fresh_db):
    db.set_player_note(A, "n")
    db.save_player_names([(A, "Stara#EUW")], NOW)
    db.save_player_names([(A, "Nowa#EUW")], NOW + 1)
    assert db.get_player_notes([A]) == {A: "n"}


def test_put_note_endpoint_and_validation(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.put(f"/api/players/{A}/note", json={"note": " flamer "}).json() == {
        "ok": True, "note": "flamer"}
    _game("EUW1_1", 1, 1, NOW, [(MY, "Ja#1", 100, 45), (A, "Zed#EUW", 100, 238)])
    assert client.get("/api/players?puuids=" + A).json()[A]["note"] == "flamer"
    assert client.get("/api/players/recurring?min_games=1").json()["players"][0]["note"] == "flamer"
    assert client.put(f"/api/players/{A}/note", json={"note": ""}).json() == {"ok": True, "note": None}
    assert client.get("/api/players?puuids=" + A).json()[A]["note"] is None
    assert client.put("/api/players/aaa/note", json={"note": "x"}).status_code == 400
    assert client.put(f"/api/players/{A}/note", json={"note": "x" * 501}).status_code == 400
    assert client.put(f"/api/players/{A}/note", json={"note": "x" * 500}).status_code == 200
    assert client.put(f"/api/players/{A}/note", json={"note": 123}).status_code == 400


def test_migrate_runs_init_player_note_last(fresh_db):
    names = db.migrate()
    assert "init_player_note" in names
    assert names.index("init_player_note") > names.index("upgrade_drop_bots")
