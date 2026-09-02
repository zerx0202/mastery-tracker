"""Smoke UI (Playwright): frontend nie mial ani jednego testu, a trzy awarie
w historii byly czysto frontowe. Zakres pierwszej fali zgodnie z lista:
plakietki puli, kafelek przepustki, rozwijane wiersze ocen, zakladki.

Testy pomijaja sie same, gdy playwright/chromium nie sa zainstalowane
(CI stawia tylko requirements.txt) - lokalnie zywia sie z .venv.
Serwer: prawdziwy uvicorn na porcie efemerycznym, baza z fresh_db,
ruch do internetu (ddragon, fonty) ucinany na poziomie przegladarki."""
import re
import threading
import time

import pytest

from app import db
from app import main as app_main
from tests.conftest import insert_row

pw = pytest.importorskip("playwright.sync_api",
                         reason="playwright niezainstalowany - smoke UI pomijam")


@pytest.fixture(scope="session")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium niedostepny (playwright install chromium): {e}")
        yield b
        b.close()


def _entry(cid, milestone):
    return {
        "championId": cid, "championLevel": 5, "championPoints": 40000,
        "lastPlayTime": int(time.time() - 86400) * 1000,
        "championSeasonMilestone": milestone, "tokensEarned": 0,
        "markRequiredForNextLevel": 2,
        "nextSeasonMilestone": {"requireGradeCounts": {"A-": 1},
                                "totalGamesRequires": 1, "rewardMarks": 1,
                                "bonus": False},
        "milestoneGrades": [],
    }


def _seed():
    """Minimalny swiat: aktywne lobby z wymiana, jedna oceniona gra,
    stan przepustki - tyle, ile trzeba, zeby kazdy testowany element
    mial co renderowac."""
    now = int(time.time())
    db.save_champions([(45, "Veigar", "Veigar"), (12, "Alistar", "Alistar"),
                       (99, "Lux", "Lux")])
    entries = [_entry(45, 1), _entry(12, 0), _entry(99, 2)]
    db.learn_ladder(entries, now)
    db.save_snapshot(now, entries)
    db.set_lobby([45, 12, 99], "KIWI", "limited", now, trade_ids=[12])
    db.set_json_setting("pass_state", {"ts": now, "events": [{
        "name": "Season 3: Act I", "days_left": 21.5,
        "progress": {"level": 3, "totalLevels": 20},
        "unclaimed": {"rewardsCount": 0}}]})
    with db.connect() as con:
        insert_row(con, "match_player", match_id="EUW1_500", game_mode="KIWI",
                   queue_id=2400, duration=1200,
                   game_creation=(now - 3600) * 1000, champion_id=45,
                   kills=9, deaths=3, assists=12, gold=14000,
                   dmg_champ=30000, cs=50, win=1)
        insert_row(con, "grade_observation", match_id="EUW1_500", game_id=500,
                   champion_id=45, grade="A-", observed_at=now - 3000)


@pytest.fixture()
def ui_server(fresh_db, monkeypatch):
    # front pyta /grades/history bez parametru mode - na produkcji tryb
    # domyslny daje env, w testach musi dac monkeypatch, inaczej widok
    # Oceny jest zawsze pusty
    monkeypatch.setattr(app_main, "DEFAULT_MODE", "KIWI")
    _seed()

    import uvicorn
    config = uvicorn.Config(app_main.app, host="127.0.0.1", port=0,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            pytest.fail("uvicorn nie wstal w 15 s")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def page(browser, ui_server):
    ctx = browser.new_context()
    pg = ctx.new_page()
    # hermetycznosc: ddragon i fonty maja nie wychodzic w swiat
    pg.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), lambda r: r.abort())
    pg.goto(ui_server + "/", wait_until="domcontentloaded")
    yield pg
    ctx.close()


def test_tabs_switch_views(page):
    page.wait_for_selector("#v-now:not([hidden])")
    page.click('nav a[href="#/oceny"]')
    page.wait_for_selector("#v-grades:not([hidden])")
    assert page.is_hidden("#v-now")
    page.click('nav a[href="#/split"]')
    page.wait_for_selector("#v-split:not([hidden])")
    assert page.is_hidden("#v-grades")
    # aktywna zakladka dostaje klase "on"
    assert "on" in page.get_attribute('nav a[href="#/split"]', "class")


def test_pool_badges_show_trade(page):
    # aktywne lobby: Alistar (12) jest z wymiany -> plakietka "wymiana"
    badge = page.wait_for_selector(".mini-badge.trade")
    assert badge.inner_text() == "wymiana"
    # plakietka zyje w wierszu wlasciwego championa
    row = page.locator("tr", has=page.locator(".mini-badge.trade"))
    assert "Alistar" in row.inner_text()


def test_pass_tile_renders(page):
    tile = page.wait_for_selector('#side .panel:has-text("Przepustki")')
    txt = tile.inner_text()
    assert "Season 3: Act I" in txt
    assert "za 21 dni" in txt
    assert "Tempo (7 dni)" in txt


def test_grade_row_expands_and_collapses(page):
    page.click('nav a[href="#/oceny"]')
    row = page.wait_for_selector("tr.grade-row")
    row.click()
    page.wait_for_selector("tr.explain-row")
    row.click()
    page.wait_for_selector("tr.explain-row", state="detached")
