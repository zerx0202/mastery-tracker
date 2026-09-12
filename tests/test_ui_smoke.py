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
    db.set_setting("ddragon_patch", "16.16.1")
    db.set_json_setting("mayhem_balance", {
        "fetched_at": now, "count": 3, "unmatched": [], "champions": {
            "45": {"Damage Dealt": "-7%", "Damage Received": "+10%"},
            "12": {"Healing": "+20%"},
            "99": {"Damage Dealt": "-5%"}}})
    db.set_json_setting("pass_state", {"ts": now, "events": [{
        "name": "Season 3: Act I", "days_left": 21.5,
        "progress": {"level": 3, "totalLevels": 20},
        "unclaimed": {"rewardsCount": 0}}]})
    # (49) sciaga z cache (patch "16.16" = seed); sam wiersz live siejemy
    # dopiero PO starcie serwera - lifespan robi migrate(), a init_live
    # celowo DROP-uje live_game
    db.set_json_setting("cheatsheet", {"99": {
        "champion_id": 99, "patch": "16.16", "fetched_at": now, "ok": True, "v": 3,
        "tier": "A", "win_rate": 51.2,
        "augments": ["Eureka", "High Roller", "Recursion"],
        "skill_sequence": "Q W E Q Q R", "skill_priority": "Q > E > W"}})
    # (G/I) notki patcha z cache - bez seeda backend testowy poszedlby po
    # prawdziwy artykul Riota; blok ma kazdy champion seeda, bo "notki"
    # pokazuja sie tylko przy bloku, a hero wybiera ranking
    db.set_json_setting("patch_notes", {
        "patch": "16.16", "fetched_at": now, "ok": True, "v": 2,
        "url": "https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-16-notes",
        "champions": {slug: {
            "name": name, "summary": f"{name} needs help.", "verdict": "buff",
            "changes": [{"ability": "Q", "label": "Cooldown", "before": "8s",
                         "after": "7s", "kind": "buff", "flag": None}]}
            for slug, name in (("veigar", "Veigar"), ("alistar", "Alistar"),
                               ("lux", "Lux"))},
        "mayhem": {}})
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
    db.set_live({"champion_id": 99, "champion": "Lux", "game_mode": "KIWI",
                 "game_time": 300.0, "kills": 2, "deaths": 0, "assists": 3,
                 "cs": 20, "ward_score": 0.0, "gold_est": 2500, "level": 6,
                 "payload": "{}", "updated_at": int(time.time())})
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
    # (H) Split w ukladzie deck: drabinka w bocznej kolumnie
    page.wait_for_selector('#v-split .deck aside .panel:has-text("Drabinka")')
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


def test_hero_links_to_patch_notes(page):
    # (41 -> G) "notki" przy nazwie prowadza do bloku championa w notkach
    # Riota (kotwica #patch-<slug> tylko, gdy champion ma blok; inaczej sam
    # artykul); wiki zostala fallbackiem bez notek w cache. Baner tez ma
    # .patch-link (bez kotwicy) - celujemy w link przy nazwie
    a = page.wait_for_selector("#hero .who a.patch-link")
    href = a.get_attribute("href")
    assert re.fullmatch(
        r"https://www\.leagueoflegends\.com/en-us/news/game-updates/"
        r"patch-26-16-notes#patch-(veigar|alistar|lux)", href), href
    block = page.wait_for_selector("#hero .patch-notes")
    txt = block.inner_text()
    assert "Patch 16.16" in txt and "buff" in txt, txt


def test_hero_shows_mayhem_balance_line(page):
    # (48) kazdy champion seeda ma mnozniki, wiec linia jest niezaleznie
    # od tego, kto wygral ranking i zostal hero
    line = page.wait_for_selector('#hero .range:has-text("Mayhem:")')
    txt = line.inner_text()
    assert "obrażenia" in txt or "leczenie" in txt
    assert "%" in txt


def test_live_panel_shows_cheatsheet(page):
    # (49) sciaga granego championa w panelu live: tier, skille, augmenty
    panel = page.wait_for_selector('#live-panel .kv:has-text("Mayhem tier")')
    assert "51.2% WR" in panel.inner_text()
    page.wait_for_selector('#live-panel .kv:has-text("Q > E > W")')
    aug = page.wait_for_selector('#live-panel .kv:has-text("Top augmenty")')
    assert "Eureka" in aug.inner_text()


def test_system_shows_gates_and_pipeline(page):
    # (P4/P8) zakladka System: liczniki bramek i zdrowie potoku
    page.click('nav a[href="#/system"]')
    panel = page.wait_for_selector('#v-system .panel:has-text("Bramki danych")')
    assert "/120" in panel.inner_text()         # (T) powtorka zmeczenia przy 120
    page.wait_for_selector('#v-system .kv:has-text("Oceny bez meczu")')
    page.wait_for_selector('#v-system .kv:has-text("Ostatni backup")')
    # (42) konsola LCU renderuje sie z polem sciezki i przyciskiem
    page.wait_for_selector("#probe-path")
    page.wait_for_selector("#probe-run")


def test_grade_row_expands_and_collapses(page):
    page.click('nav a[href="#/oceny"]')
    row = page.wait_for_selector("tr.grade-row")
    row.click()
    page.wait_for_selector("tr.explain-row")
    row.click()
    page.wait_for_selector("tr.explain-row", state="detached")


def test_champ_select_bar_shows_ally_chips(page):
    # (Q) sojusznicy jako zetony z ikona i historia zamiast szarego tekstu;
    # nowy gracz dostaje "nowy", ukryty "(ukryty)"
    now = int(time.time())
    db.set_lobby([45, 12, 99], "KIWI", "limited", now, trade_ids=[12], allies=[
        {"cellId": 1, "championId": 12, "puuid": "a" * 36, "name": "Zed#EUW", "hidden": False},
        {"cellId": 3, "championId": 99, "puuid": "", "name": "", "hidden": True}])
    page.reload()
    chips = page.wait_for_selector("#live-bar .allies")
    txt = chips.inner_text()
    assert "Zed" in txt and "nowy" in txt and "(ukryty)" in txt, txt
    assert page.locator("#live-bar .ally img").count() == 2


def test_ally_chip_opens_note_form_and_saves(page):
    # (V/W) notatka o graczu: klik w zeton otwiera formularz w <dialog>
    # (zamiast window.prompt), zapis idzie z formularza, zeton odswieza sie
    # od razu; skrot w zetonie + pelny tekst w title
    now = int(time.time())
    db.set_lobby([45, 12, 99], "KIWI", "limited", now, trade_ids=[12], allies=[
        {"cellId": 1, "championId": 12, "puuid": "a" * 36, "name": "Zed#EUW", "hidden": False},
        {"cellId": 3, "championId": 99, "puuid": "", "name": "", "hidden": True}])
    full = "flamer, mutuj od startu i graj swoje - dluga notatka"
    db.set_player_note("a" * 36, full)
    page.reload()
    chip = page.wait_for_selector("#live-bar .ally.noted")
    assert "flamer" in chip.inner_text()
    note = chip.query_selector(".note")
    assert note.get_attribute("title") == full and note.inner_text().endswith("…")
    assert page.locator("#live-bar .ally[data-puuid]").count() == 1   # ukryty bez puuid
    page.click("#live-bar .ally[data-puuid]")
    page.wait_for_selector("#note-dlg[open]")
    assert page.input_value("#note-text") == full
    assert "Zed" in page.inner_text("#note-who")
    page.fill("#note-text", "nowa notatka")
    page.click("#note-save")
    page.wait_for_selector("#note-dlg", state="hidden")
    page.wait_for_selector('#live-bar .note:has-text("nowa notatka")')
    assert db.get_player_notes(["a" * 36]) == {"a" * 36: "nowa notatka"}
    # "Usun" kasuje wiersz i zeton traci notatke
    page.click("#live-bar .ally[data-puuid]")
    page.wait_for_selector("#note-dlg[open]")
    page.click("#note-del")
    page.wait_for_selector("#note-dlg", state="hidden")
    page.wait_for_selector("#live-bar .ally.noted", state="detached")
    assert db.get_player_notes(["a" * 36]) == {}


def test_api_token_persists_without_running_a_probe(page):
    # (W) token wpisany w Systemie zapisuje sie od razu - dotad dopiero
    # przycisk "Wyslij" go utrwalal, wiec po wyjsciu z zakladki znikal
    page.click('nav a[href="#/system"]')
    page.wait_for_selector("#probe-token")
    page.wait_for_selector('#token-state:has-text("brak")')
    page.fill("#probe-token", "sekret")
    page.wait_for_selector('#token-state:has-text("zapisany")')
    page.click('nav a[href="#/"]')
    page.wait_for_selector("#v-now:not([hidden])")
    page.click('nav a[href="#/system"]')
    page.wait_for_selector("#probe-token")
    assert page.input_value("#probe-token") == "sekret"
    assert page.evaluate("localStorage.getItem('api_token')") == "sekret"


def test_ranking_row_click_shows_that_champion_in_hero(page):
    # (W) klik w wiersz rankingu = ta sama karta hero dla tego championa
    # (numer z rankingu, tabela bez niego), "lider" wraca do lidera
    row = page.wait_for_selector("#cards tr[data-pick]")
    name = row.query_selector(".champ-cell").inner_text().split()[0]
    row.click()
    page.wait_for_selector('#hero .rank-badge:has-text("2")')
    assert name in page.inner_text("#hero .who")
    page.wait_for_selector('#cards tr[data-pick] .rank-cell:has-text("1")')
    page.click("#hero .hero-back")
    page.wait_for_selector('#hero .rank-badge:has-text("1")')
    page.wait_for_selector("#hero .hero-back", state="detached")


def test_tick_keeps_dom_nodes_alive(page):
    # (W) morph zamiast innerHTML: po kilku tickach (co 1 s w champ selekcie)
    # tabela, wiersz i pasek champ selecta to wciaz te same wezly - dotad
    # kazdy tick tworzyl je od nowa i ekran migal
    page.wait_for_selector("#cards tr[data-pick]")
    page.wait_for_selector("#live-bar .live")
    page.evaluate("""() => {
        document.querySelector('#cards table').__keep = 1;
        document.querySelector('#cards tr[data-pick]').__keep = 1;
        document.querySelector('#live-bar .live').__keep = 1; }""")
    page.wait_for_timeout(2600)
    assert page.evaluate("document.querySelector('#cards table').__keep") == 1
    assert page.evaluate("document.querySelector('#cards tr[data-pick]').__keep") == 1
    assert page.evaluate("document.querySelector('#live-bar .live').__keep") == 1


def test_lab_recurring_players_show_old_ids_without_footnote(page):
    # (W) "Powtarzajacy sie gracze" bez dopisku (karta 9) i bez stopki;
    # dawne Riot ID pod biezaca nazwa
    from tests.test_partia_m import _game
    my, a = "m" * 36, "a" * 36
    now = int(time.time())
    _game("EUW1_1", 1, 1, now - 7200, [(my, "Ja#1", 100, 45), (a, "Stara#EUW", 100, 238)])
    _game("EUW1_2", 2, 0, now - 3600, [(my, "Ja#1", 200, 45), (a, "Nowa#EUW", 100, 238)])
    db.save_player_names([(a, "Nowa#EUW")], now + 100)
    page.click('nav a[href="#/lab"]')
    panel = page.wait_for_selector('#v-lab .panel:has-text("Powtarzający się gracze")')
    txt = panel.inner_text()
    assert "(karta 9)" not in txt and "Tożsamości" not in txt
    assert "Nowa" in txt and "dawniej Stara#EUW" in txt
