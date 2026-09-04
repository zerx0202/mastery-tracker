"""(49) Sciaga-z-danych: parser strony buildu arammayhem + endpoint
z cache per patch. Fixture'y to doslowne formaty z zywej strony 2.09
(meta description, JSON-LD ItemList, tekst Skill Sequence)."""
from fastapi.testclient import TestClient

from app import balance, db
from app.main import app

BUILD_HTML = (
    '<meta name="description" content="Veigar ARAM Mayhem build for '
    'Patch 26.17: best items, augments, runes, and combos. A tier with '
    '50.49% win rate.">'
    '<script type="application/ld+json">{"@context":"https://schema.org",'
    '"@type":"ItemList","name":"Best Augments for Veigar",'
    '"itemListElement":[{"@type":"ListItem","position":1,"name":"Eureka"},'
    '{"@type":"ListItem","position":2,"name":"Jeweled Gauntlet"},'
    '{"@type":"ListItem","position":3,"name":"High Roller"}]}</script>'
    '<span>Skill Sequence: Q W E Q Q R Q E Q E R E E W W</span>'
)


def test_parse_build_extracts_everything():
    out = balance.parse_build(BUILD_HTML)
    assert out["site_patch"] == "26.17"
    assert out["tier"] == "A" and out["win_rate"] == 50.49
    assert out["augments"] == ["Eureka", "Jeweled Gauntlet", "High Roller"]
    assert out["skill_sequence"].startswith("Q W E Q Q R")
    # Q=5, E=5 (Q wczesniej), W=3
    assert out["skill_priority"] == "Q > E > W"


def test_parse_build_garbage_gives_empty():
    assert balance.parse_build("<html>przebudowa strony</html>") == {}


def test_cheatsheet_endpoint_serves_fresh_cache(fresh_db):
    # ddragon_patch nieustawiony -> short=None; wpis z patch=None jest swiezy
    db.set_json_setting("cheatsheet", {"45": {
        "champion_id": 45, "patch": None, "fetched_at": 1, "ok": True, "v": 3,
        "tier": "S", "win_rate": 55.5, "augments": ["Eureka"],
        "skill_priority": "Q > E > W"}})
    client = TestClient(app, raise_server_exceptions=False)
    out = client.get("/api/cheatsheet/45").json()
    assert out["ok"] is True and out["tier"] == "S"


def test_cheatsheet_unknown_champion_404(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/cheatsheet/999").status_code == 404


def test_cheatsheet_stale_patch_not_served_from_cache(fresh_db):
    # wpis z poprzedniego patcha, swiezy ddragon -> cache niewazny;
    # fetch pojdzie po state["plain"], ktorego w testach nie ma -> ok:False
    db.set_setting("ddragon_patch", "16.17.1")
    db.save_champions([(45, "Veigar", "Veigar")])
    db.set_json_setting("cheatsheet", {"45": {
        "champion_id": 45, "patch": "16.16", "fetched_at": 1, "ok": True,
        "tier": "S"}})
    client = TestClient(app, raise_server_exceptions=False)
    out = client.get("/api/cheatsheet/45").json()
    assert out["ok"] is False and out["patch"] == "16.17"
    # nieudany fetch zapisany jako negative-cache
    assert db.get_json_setting("cheatsheet")["45"]["ok"] is False
