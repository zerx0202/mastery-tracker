"""Partia G (4.09): zmiany championa w biezacym patchu inline z werdyktem
buff/nerf - zrodlo: oficjalne notki Riota (wiki za Cloudflare'em, 403
z serwera). Markup fixture'a 1:1 z sondy artykulu 26.17."""
import time

from fastapi.testclient import TestClient

from app import db, patchnotes
from app.main import app

ARROW = patchnotes.ARROW
ARTICLE = (
    '<h2 id="patch-highlights">Patch Highlights</h2><p>x</p>'
    '<h2 id="patch-champions">Champions</h2></header>'
    '<div class="patch-change-block white-stone accent-before"><div>'
    '<h3 class="change-title" id="patch-aurelionsol"><a href="/x">Aurelion Sol</a></h3>'
    '<blockquote class="blockquote context"><p>Aurelion Sol has come out the loser '
    'of last patch&#39;s mage adjustments, so we are giving him a buff.&nbsp;</p>'
    '<hr class="divider"><h4 class="change-detail-title ability-title"><img src="q.png">'
    'Q - Breath of Light</h4></blockquote>'
    f'<ul><li><strong>Mana Cost Per Second</strong>: 35 / 40 / 45 / 50 / 55 {ARROW} '
    '<strong>30 / 35 / 40 / 45 / 50</strong></li></ul><hr class="divider">'
    '<h4 class="change-detail-title ability-title"><img src="w.png">W - Astral Flight</h4>'
    f'<ul><li><strong>Cooldown</strong>: 22 / 20.5 / 19 / 17.5 / 16s {ARROW} '
    '<strong>22 / 20 / 18 / 16 / 14s</strong></li></ul></div></div>'
    '<div class="patch-change-block"><div>'
    '<h3 class="change-title" id="patch-nocturne"><a href="/y">Nocturne</a></h3>'
    '<blockquote class="blockquote context"><p>Nocturne has been strong.</p></blockquote>'
    '<hr class="divider"><h4 class="change-detail-title">Base Stats</h4>'
    f'<ul><li><strong>Armor</strong>: 38 {ARROW} <strong>36</strong></li>'
    f'<li><strong>Health</strong>: 655 {ARROW} <strong>640</strong></li></ul></div></div>'
    '<div class="patch-change-block"><div>'
    '<h3 class="change-title" id="patch-vayne"><a href="/v">Vayne</a></h3>'
    '<h4 class="change-detail-title ability-title">R - Final Hour</h4>'
    '<ul><li><span style="background-color:#53ad56;"><strong>&nbsp; NEW &nbsp;</strong>'
    '</span>&nbsp;<strong>Effect</strong>: Tumble grants stealth</li>'
    f'<li><strong>Bonus AD</strong>: 25 / 40 / 55 {ARROW} <strong>30 / 40 / 50</strong></li>'
    '</ul></div></div>'
    '<h2 id="patch-items">Items</h2>'
    '<div class="patch-change-block"><div>'
    '<h3 class="change-title" id="patch-stormrazor"><a href="/s">Stormrazor</a></h3>'
    f'<ul><li><strong>Attack Damage</strong>: 55 {ARROW} <strong>60</strong></li></ul></div></div>'
    '<h2 id="patch-aram:-mayhem">ARAM: Mayhem</h2>'
    '<h4 class="change-detail-title">Champions</h4>'
    f'<p><strong>Ornn</strong></p><ul><li><strong>Damage Dealt</strong>: 95% {ARROW} '
    '<strong>92%</strong></li></ul><hr class="divider">'
    '<p><strong>Kog&#39;Maw</strong></p><ul><li><strong>Damage Taken</strong>: '
    f'105% {ARROW} <strong>100%</strong></li></ul>'
    '<h4 class="change-detail-title">Augments</h4>'
    f'<p><strong>Double Tap</strong></p><ul><li><strong>Tier</strong>: Gold {ARROW} '
    '<strong>Prismatic</strong></li></ul>'
    '<h2 id="patch-arena">Arena</h2><p>nic</p>'
)
LISTING = (
    '<a href="/en-us/news/game-updates/league-of-legends-patch-26-17-notes">26.17</a>'
    '<a href="/en-us/news/game-updates/patch-25-05-notes/">25.5</a>'
)
ART_URL = "https://www.leagueoflegends.com/en-us/news/game-updates/league-of-legends-patch-26-17-notes"


def test_classify_lines():
    c = patchnotes.classify
    assert c("Cooldown", "22 / 20.5 / 19s", "22 / 20 / 18s") == "buff"
    assert c("Mana Cost Per Second", "35 / 40", "30 / 35") == "buff"
    assert c("Armor", "38", "36") == "nerf"
    assert c("AP Ratio", "60%", "70%") == "buff"
    assert c("Tier", "Gold", "Prismatic") == "adjust"                    # bez liczb
    assert c("Bonus AD", "25 / 40 / 55", "30 / 40 / 50") == "adjust"      # w obie strony
    assert c("Missile Amount", "1 / 2 / 3 / 4 based", "1 / 2 / 3 based") == "adjust"
    assert c("Effect", None, "Gain a Tiamat") == "adjust"
    # ulamek bez zera wiodacego + ruch w obie strony (Vayne 26.17)
    assert c("Attack Speed Ratio", ".658 + 3.3%/Level", ".67 + 2.8%/Level") == "adjust"
    assert c("Base Attack Speed", ".625", ".65") == "buff"


def test_verdict_aggregates_kinds():
    v = patchnotes.verdict
    assert v([{"kind": "buff"}, {"kind": "adjust"}]) == "buff"
    assert v([{"kind": "nerf"}]) == "nerf"
    assert v([{"kind": "buff"}, {"kind": "nerf"}]) == "mixed"
    assert v([{"kind": "adjust"}]) == "adjust" and v([]) == "adjust"


def test_parse_notes_scopes_sections_and_reads_blocks():
    out = patchnotes.parse_notes(ARTICLE)
    assert set(out["champions"]) == {"aurelionsol", "nocturne", "vayne"}   # bez Items
    asol = out["champions"]["aurelionsol"]
    assert asol["name"] == "Aurelion Sol" and asol["verdict"] == "buff"
    assert asol["summary"].startswith("Aurelion Sol has come out the loser of last patch's")
    q, w = asol["changes"]
    assert (q["ability"], q["label"], q["kind"]) == ("Q - Breath of Light",
                                                     "Mana Cost Per Second", "buff")
    assert q["before"] == "35 / 40 / 45 / 50 / 55" and q["after"] == "30 / 35 / 40 / 45 / 50"
    assert (w["ability"], w["kind"]) == ("W - Astral Flight", "buff")
    noc = out["champions"]["nocturne"]
    assert noc["verdict"] == "nerf"
    assert [c["ability"] for c in noc["changes"]] == ["Base Stats", "Base Stats"]
    vay = out["champions"]["vayne"]
    assert vay["changes"][0]["flag"] == "new" and vay["changes"][0]["label"] == "Effect"
    assert vay["changes"][0]["kind"] == "adjust" and vay["verdict"] == "adjust"
    # sekcja trybu: championy tak, augmenty nie
    assert set(out["mayhem"]) == {"ornn", "kogmaw"}
    assert out["mayhem"]["ornn"]["verdict"] == "nerf"
    assert out["mayhem"]["ornn"]["changes"][0]["ability"] == "Mayhem"


def test_parse_notes_garbage_is_empty():
    assert patchnotes.parse_notes("<html>przebudowa</html>") == {
        "champions": {}, "mayhem": {}, "mayhem_augments": {}}


def test_find_article_url_discovers_both_slug_formats():
    f = patchnotes.find_article_url
    assert f(LISTING, "16.17") == ART_URL
    assert f(LISTING, "15.5") == (
        "https://www.leagueoflegends.com/en-us/news/game-updates/patch-25-05-notes/")
    assert f(LISTING, "16.18") is None and f(LISTING, None) is None


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class _Plain:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(*self.pages.get(url, ("", 404)))


def _world(monkeypatch, pages):
    from app import main
    db.set_setting("ddragon_patch", "16.17.1")
    db.save_champions([(136, "Aurelion Sol", "AurelionSol"), (56, "Nocturne", "Nocturne"),
                       (516, "Ornn", "Ornn"), (45, "Veigar", "Veigar")])
    plain = _Plain(pages)
    monkeypatch.setitem(main.state, "plain", plain)
    return plain


def test_patchnotes_endpoint_fetches_once_and_maps_champions(fresh_db, monkeypatch):
    plain = _world(monkeypatch, {patchnotes.NEWS_URL: (LISTING, 200),
                                 ART_URL: (ARTICLE, 200)})
    client = TestClient(app, raise_server_exceptions=False)
    out = client.get("/api/patchnotes/136").json()
    assert out["ok"] and out["patch"] == "16.17"
    assert out["champion"]["verdict"] == "buff"
    assert out["anchor_url"] == ART_URL + "#patch-aurelionsol"
    assert out["mayhem"] is None
    # Ornn: brak bloku w Champions, jest w sekcji Mayhem
    orn = client.get("/api/patchnotes/516").json()
    assert orn["champion"] is None and orn["mayhem"]["verdict"] == "nerf"
    # Veigar: patch go nie dotyczy - ok, ale pusto (front pisze "bez zmian")
    vg = client.get("/api/patchnotes/45").json()
    assert vg["ok"] and vg["champion"] is None and vg["mayhem"] is None
    allv = client.get("/api/patchnotes").json()
    assert allv["verdicts"] == {"136": "buff", "56": "nerf", "516": "nerf"}
    assert plain.calls == [patchnotes.NEWS_URL, ART_URL]     # jeden fetch na patch


def test_patchnotes_failed_fetch_is_negative_cached(fresh_db, monkeypatch):
    plain = _world(monkeypatch, {patchnotes.NEWS_URL: ('<a href="/nic">x</a>', 200)})
    client = TestClient(app, raise_server_exceptions=False)
    out = client.get("/api/patchnotes/136").json()
    assert out["ok"] is False and "brak artykulu" in out["reason"]
    client.get("/api/patchnotes/136")
    assert plain.calls == [patchnotes.NEWS_URL]              # godzina ciszy
    st = db.get_json_setting("patch_notes")
    assert st["fetched_at"] <= int(time.time()) and st["champions"] == {}


def test_patchnotes_refresh_endpoint(fresh_db, monkeypatch):
    plain = _world(monkeypatch, {patchnotes.NEWS_URL: (LISTING, 200),
                                 ART_URL: (ARTICLE, 200)})
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/patchnotes/refresh").json()
    assert r["ok"] and r["champions"] == 3
    r = client.post("/api/patchnotes/refresh").json()
    assert r["ok"] and len(plain.calls) == 4                 # refresh wymusza fetch
