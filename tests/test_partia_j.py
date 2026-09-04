"""Partia J (4.09): (3) sciaga z porad i profilu Riota (Data Dragon pl_PL
+ CommunityDragon) obok danych arammayhem - trzy zrodla, kazde osobno;
(5) zmiany augmentow Mayhema z sekcji "ARAM: Mayhem" notek; (6) licznik
gier poza misja w System."""
from fastapi.testclient import TestClient

from app import balance, champinfo, db, patchnotes
from app.main import app
from tests.conftest import insert_row
from tests.test_partia_g import ART_URL, ARTICLE, LISTING, _world

DD = {"data": {"KogMaw": {
    "tags": ["Marksman", "Mage"],
    "info": {"attack": 8, "defense": 2, "magic": 5, "difficulty": 6},
    "allytips": ["Kog'Maw ma większy zasięg niż większość bohaterów.",
                 "Użyj Szlamu Pustki, aby zapewnić trafienie Żywym Pociskiem.",
                 "  ", "trzecia", "czwarta"],
    "enemytips": ["Kog'Maw nie posiada dobrej zdolności ucieczki.",
                  "Oddal się od Kog'Mawa po jego śmierci!"]}}}
CD = {"playstyleInfo": {"damage": 3, "durability": 1, "crowdControl": 1,
                        "mobility": 1, "utility": 1},
      "tacticalInfo": {"style": 2, "difficulty": 2, "damageType": "kMixed",
                       "attackType": "ranged"},
      "roles": ["marksman", "mage"]}


def test_parse_dd_and_cdragon():
    dd = champinfo.parse_dd(DD, "KogMaw")
    assert dd["tips_ally"] == ["Kog'Maw ma większy zasięg niż większość bohaterów.",
                               "Użyj Szlamu Pustki, aby zapewnić trafienie Żywym Pociskiem.",
                               "trzecia"]                       # pusty odpada, max 3
    assert dd["tips_enemy"][0].startswith("Kog'Maw nie posiada")
    assert dd["tags"] == ["Marksman", "Mage"] and dd["info"]["attack"] == 8
    assert champinfo.parse_dd(DD, "Veigar") == {}
    cd = champinfo.parse_cdragon(CD)
    assert cd["playstyle"] == {"damage": 3, "durability": 1, "crowdControl": 1,
                               "mobility": 1, "utility": 1}
    assert cd["tactical"] == {"style": 2, "difficulty": 2, "damage": "mieszane",
                              "ranged": True}
    assert champinfo.parse_cdragon({"roles": []}) == {}


class _Resp:
    def __init__(self, status, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        return self._data


class _Plain:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def get(self, url, **kw):
        self.calls.append(url)
        return self.pages.get(url, _Resp(404))


def test_cheatsheet_merges_three_sources_and_survives_build_failure(fresh_db, monkeypatch):
    from app import main
    db.set_setting("ddragon_patch", "16.17.1")
    db.save_champions([(96, "Kog'Maw", "KogMaw")])
    plain = _Plain({
        champinfo.DD_CHAMPION_URL.format(version="16.17.1", key="KogMaw"): _Resp(200, data=DD),
        champinfo.CDRAGON_CHAMPION_URL.format(id=96): _Resp(200, data=CD),
        # strona buildu padla (404) - reszta sciagi ma zyc
    })
    monkeypatch.setitem(main.state, "plain", plain)
    client = TestClient(app, raise_server_exceptions=False)
    out = client.get("/api/cheatsheet/96").json()
    assert out["ok"] is True and out["v"] == main._CHEAT_V and "tier" not in out
    assert out["tips_ally"][0].startswith("Kog'Maw ma") and out["playstyle"]["damage"] == 3
    assert out["tactical"]["ranged"] is True
    assert balance.BUILD_URL.format(slug="kogmaw") in plain.calls and len(plain.calls) == 3
    # drugie odpytanie z cache - zero fetchy
    client.get("/api/cheatsheet/96")
    assert len(plain.calls) == 3


def test_parse_notes_collects_mayhem_augment_changes():
    out = patchnotes.parse_notes(ARTICLE)
    assert set(out["mayhem"]) == {"ornn", "kogmaw"}            # championy trybu
    assert list(out["mayhem_augments"]) == ["doubletap"]      # augmenty osobno
    dt = out["mayhem_augments"]["doubletap"]
    assert dt["name"] == "Double Tap" and dt["verdict"] == "adjust"
    assert dt["changes"][0]["label"] == "Tier" and dt["changes"][0]["after"] == "Prismatic"
    assert patchnotes.parse_notes("<html>nic</html>") == {
        "champions": {}, "mayhem": {}, "mayhem_augments": {}}


def test_patchnotes_all_exposes_augment_changes(fresh_db, monkeypatch):
    _world(monkeypatch, {patchnotes.NEWS_URL: (LISTING, 200), ART_URL: (ARTICLE, 200)})
    client = TestClient(app, raise_server_exceptions=False)
    allv = client.get("/api/patchnotes").json()
    assert allv["augments"]["doubletap"]["verdict"] == "adjust"
    # wpis cache sprzed tej wersji (bez augmentow) idzie do odswiezenia
    st = db.get_json_setting("patch_notes")
    assert st["v"] == 2


def test_health_counts_games_outside_mission(fresh_db):
    with db.connect() as con:
        for mid, mode in (("EUW1_1", "KIWI"), ("EUW1_2", "KIWI"), ("EUW1_3", "CLASSIC"),
                          ("EUW1_4", "PRACTICETOOL"), ("EUW1_5", "KIWI_CUSTOM")):
            insert_row(con, "match_player", match_id=mid, champion_id=1,
                       duration=1200, game_mode=mode)
    client = TestClient(app, raise_server_exceptions=False)
    h = client.get("/api/system/health").json()
    assert h["counts"]["match_player"] == 5
    assert h["non_mission_games"] == 3 and h["custom_games"] == 1
