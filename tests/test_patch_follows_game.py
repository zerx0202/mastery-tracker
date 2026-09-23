"""Biezacy patch (baner, liczba gier na patchu, notki, sciaga, slownik
augmentow) idzie za gra, nie tylko za Data Dragonem (zgloszenie 23.09:
gra na 16.19, system na 16.17). Dwie przyczyny: wersje DD odswiezal
wylacznie reczny POST /refresh-champions (23 dni bez wywolania), a sam DD
publikuje nowy patch z opoznieniem (sonda 23.09: najnowszy 16.18.1 przy
grze na 16.19). Patch ostatniej wlasnej gry pochodzi z gameVersion klienta."""
import asyncio

from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from tests.conftest import insert_row


def _game(mid, patch, created):
    with db.connect() as con:
        insert_row(con, "match_player", match_id=mid, game_mode="KIWI",
                   queue_id=2400, duration=1200, game_creation=created,
                   champion_id=45, patch=patch)


def test_current_patch_follows_newest_game_when_data_dragon_lags(fresh_db):
    db.set_setting("ddragon_patch", "16.18.1")
    _game("EUW1_1", "16.18", 1_790_000_000_000)
    _game("EUW1_2", "16.19", 1_790_100_000_000)
    meta = app_main.patch_meta("KIWI")
    assert meta["short"] == "16.19" and meta["games"] == 1
    assert meta["version"] == "16.18.1"          # wersja zasobow DD bez zmian


def test_current_patch_stays_on_data_dragon_before_first_game(fresh_db):
    db.set_setting("ddragon_patch", "16.19.1")
    _game("EUW1_1", "16.18", 1_790_000_000_000)
    assert app_main.patch_meta("KIWI")["short"] == "16.19"


def test_newest_game_is_found_across_ms_and_s_timestamps(fresh_db):
    # game_creation bywa w ms i w s - gra w sekundach jest tu NOWSZA
    db.set_setting("ddragon_patch", "16.17.1")
    _game("EUW1_1", "16.18", 1_790_000_000_000)   # ms
    _game("EUW1_2", "16.19", 1_790_100_000)       # s, pozniej
    assert db.latest_game_patch() == "16.19"


def test_patch_versions_compare_numerically():
    assert app_main.newer_patch("16.9", "16.10") == "16.10"
    assert app_main.newer_patch(None, "16.19") == "16.19"
    assert app_main.newer_patch("16.19", None) == "16.19"


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, "{}"

    def json(self):
        return self._data


class FakePlain:
    def __init__(self, versions):
        self.versions = versions
        self.urls = []

    async def get(self, url, **kw):
        self.urls.append(url)
        if url.endswith("versions.json"):
            return FakeResp(self.versions)
        if url.endswith("champion.json"):
            return FakeResp({"data": {"Veigar": {"key": "45", "name": "Veigar",
                                                 "id": "Veigar", "tags": ["Mage"]}}})
        return FakeResp({}, 404)


def test_data_dragon_refresh_picks_up_new_version(fresh_db, monkeypatch):
    db.set_setting("ddragon_patch", "16.17.1")
    fake = FakePlain(["16.18.1", "16.17.1"])
    monkeypatch.setitem(app_main.state, "plain", fake)
    r = asyncio.run(app_main.refresh_champions())
    assert r["patch"] == "16.18.1" and db.get_setting("ddragon_patch") == "16.18.1"
    again = asyncio.run(app_main.refresh_champions())
    assert again.get("skipped") is True


def test_lifespan_starts_data_dragon_refresh_loop(fresh_db, monkeypatch):
    started = []

    async def fake_loop():
        started.append(1)
    monkeypatch.setattr(app_main, "ddragon_refresh_loop", fake_loop)
    with TestClient(app_main.app):
        pass
    assert started == [1]


def test_augment_book_follows_current_patch(fresh_db, monkeypatch):
    # slownik augmentow z "latest" CDragona odswiezany, gdy zmieni sie patch
    # GRY - dotad czekal na recznie odswiezony Data Dragon
    db.set_setting("ddragon_patch", "16.18.1")
    _game("EUW1_2", "16.19", 1_790_100_000_000)
    db.set_json_setting("augment_book", {"patch": "16.18"})
    stored = []

    class Plain:
        async def get(self, url, **kw):
            return type("R", (), {"status_code": 200, "text": "{}"})()
    monkeypatch.setitem(app_main.state, "plain", Plain())
    monkeypatch.setattr(app_main.augments, "store_augments",
                        lambda text, short: stored.append(short) or {"ok": True})
    assert asyncio.run(app_main.augments_refresh_once()) is True
    assert stored == ["16.19"]
    db.set_json_setting("augment_book", {"patch": "16.19"})
    assert asyncio.run(app_main.augments_refresh_once()) is False
