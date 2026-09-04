"""(G) Zmiany championa w biezacym patchu - inline przy hero i w panelu live,
z werdyktem buff/nerf/mieszane/zmiany, zamiast linku do calego patcha
(uwaga czlowieka 3.09: link na wiki "daje 0 value").

Zrodlo: OFICJALNE notki Riota (www.leagueoflegends.com/en-us/news/game-updates/
<slug>). Wiki (wiki.leagueoflegends.com) odpada: HTML, action=raw i api.php
siedza za wyzwaniem JS Cloudflare'a i z serwera oddaja 403 "Please wait"
(sonda 4.09). Slug artykulu zmienil format w 2026 (patch-25-11-notes ->
league-of-legends-patch-26-17-notes), wiec URL odkrywamy z listingu
game-updates po fragmencie patch-<maj>-<min>-notes, nie z szablonu.

Markup artykulu (sonda 4.09; ten sam od lat, zyja z niego scrapery):
  <h2>Champions</h2> ... <div class="patch-change-block">
    <h3 class="change-title" id="patch-<klucz DD lowercase>">Nazwa</h3>
    <blockquote class="blockquote context"><p>uzasadnienie Riota</p>...
    <h4 class="change-detail-title ability-title">W - Astral Flight</h4>
    <ul><li><strong>Cooldown</strong>: 22 / 20.5s => <strong>22 / 20s</strong></li>
  Blok bez umiejetnosci ma <h4 class="change-detail-title">Base Stats</h4>.
  Sekcje trybow ("ARAM: Mayhem", "Classic") nie maja patch-change-block:
  championy to <h4 ...>Champions</h4> + <p><strong>Nazwa</strong></p> + lista.
  Bloki przedmiotow (sekcja Items) maja ten sam markup co championy -
  stad zakres po sekcji, nie po klasie.

Werdykt to HEURYSTYKA na liczbach: wektor "przed" vs "po" (wszystkie <=
i ktores < to spadek); dla etykiet kosztowych (cooldown, cost, mana, energy,
cast time, delay, recharge) spadek = buff, dla reszty spadek = nerf; linia
bez liczb, z NEW/REMOVED albo z ruchem w obie strony = "adjust". Champion:
buffy i nerfy naraz = mixed, same buffy = buff, same nerfy = nerf, reszta
= adjust. To streszczenie do rzutu oka w champ selekcie, nie analiza.

DECYZJA (ta sama co przy balansie, sciadze i augmentach): zrodlo zewnetrzne
sluzy WYLACZNIE do wyswietlania - nigdy jako cecha modelu ani normalizator.
"""
import html as _html
import re

NEWS_URL = "https://www.leagueoflegends.com/en-us/news/game-updates/"
SITE = "https://www.leagueoflegends.com"
HEADERS = {"User-Agent": "mastery-tracker/1.0"}
ARROW = "⇒"

# etykiety, przy ktorych NIZSZA wartosc jest lepsza dla championa
COST_WORDS = ("cooldown", "cost", "mana", "energy", "cast time", "delay",
              "recharge", "windup", "channel")

_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_BLOCK_RE = re.compile(
    r'<h3 class="change-title" id="patch-([a-z0-9]+)"[^>]*>(.*?)</h3>', re.S)
_CONTEXT_RE = re.compile(r'<blockquote class="blockquote context">\s*<p>(.*?)</p>', re.S)
_DETAIL_RE = re.compile(r'<h4 class="change-detail-title[^"]*"[^>]*>(.*?)</h4>', re.S)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S)
_ENTRY_RE = re.compile(r"<p><strong>([^<]+)</strong></p>(.*?)(?=<p><strong>|$)", re.S)
# takze ulamki bez zera wiodacego (".658 + 3.3%/Level" u Riota) - inaczej
# ".658" czyta sie jako 658 i linia dostaje falszywy kierunek
_NUM_RE = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")


def _text(s):
    t = _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", t.replace("\xa0", " ")).strip()


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def find_article_url(listing_html, short):
    """Data Dragon 16.17 -> slug patch-26-17-notes (marketing = major + 10,
    minor dopelniony zerem: patch-25-05-notes). Pierwszy pasujacy href."""
    try:
        maj, mino = short.split(".")[:2]
        frag = f"patch-{int(maj) + 10}-{int(mino):02d}-notes"
    except (AttributeError, ValueError):
        return None
    m = re.search(r'href="([^"]*' + re.escape(frag) + r'[^"]*)"', listing_html or "")
    if not m:
        return None
    url = m.group(1)
    return SITE + url if url.startswith("/") else url


def classify(label, before, after):
    """buff / nerf / adjust dla jednej linii zmiany."""
    if before is None or after is None:
        return "adjust"
    b = [float(x) for x in _NUM_RE.findall(before)]
    a = [float(x) for x in _NUM_RE.findall(after)]
    if not a or not b:
        return "adjust"
    n = min(len(a), len(b))
    diffs = [x - y for x, y in zip(a[:n], b[:n], strict=True)]
    up, down = any(d > 0 for d in diffs), any(d < 0 for d in diffs)
    if up == down:            # oba naraz albo zaden (np. skrocony wektor)
        return "adjust"
    lower_better = any(w in label.lower() for w in COST_WORDS)
    return "buff" if (down if lower_better else up) else "nerf"


def verdict(changes):
    kinds = {c["kind"] for c in changes}
    if {"buff", "nerf"} <= kinds:
        return "mixed"
    if "buff" in kinds:
        return "buff"
    if "nerf" in kinds:
        return "nerf"
    return "adjust"


def _parse_line(li_html, ability):
    text = _text(li_html)
    flag = None
    for f in ("NEW", "REMOVED", "UPDATED"):
        if text.upper().startswith(f):
            flag, text = f.lower(), text[len(f):].strip()
            break
    if ARROW in text:
        left, after = (x.strip() for x in text.split(ARROW, 1))
        label, _sep, before = left.partition(":")
        before = before.strip() or None
    else:
        label, sep, rest = text.partition(":")
        if not sep:
            label, rest = "", text
        before, after = None, rest.strip()
    label = label.strip()
    kind = "adjust" if flag else classify(label, before, after)
    return {"ability": ability, "label": label, "before": before,
            "after": after, "kind": kind, "flag": flag}


def _changes(chunk):
    """Linie zmian w kolejnosci strony, kazda z tytulem umiejetnosci
    (albo 'Base Stats'), pod ktorym stoi."""
    details = list(_DETAIL_RE.finditer(chunk))
    if not details:
        return [_parse_line(li.group(1), "") for li in _LI_RE.finditer(chunk)]
    out = [_parse_line(li.group(1), "")
           for li in _LI_RE.finditer(chunk[:details[0].start()])]
    starts = [m.start() for m in details] + [len(chunk)]
    for i, m in enumerate(details):
        title = _text(m.group(1))
        for li in _LI_RE.finditer(chunk[m.end():starts[i + 1]]):
            out.append(_parse_line(li.group(1), title))
    return out


def _section(html, name):
    heads = [(m.start(), m.end(), _text(m.group(1))) for m in _H2_RE.finditer(html or "")]
    for i, (_s, e, t) in enumerate(heads):
        if t == name:
            return html[e:heads[i + 1][0] if i + 1 < len(heads) else len(html)]
    return ""


def parse_notes(html):
    """HTML artykulu -> {champions: {slug: {...}}, mayhem: {norm(nazwa): {...}}}.
    Puste 'champions' = przebudowa markupu albo patch bez zmian championow -
    caller traktuje to jak nieudany fetch (nie nadpisuje dobrych danych)."""
    out = {"champions": {}, "mayhem": {}}
    sec = _section(html, "Champions")
    blocks = list(_BLOCK_RE.finditer(sec))
    for i, b in enumerate(blocks):
        chunk = sec[b.start():blocks[i + 1].start() if i + 1 < len(blocks) else len(sec)]
        ctx = _CONTEXT_RE.search(chunk)
        changes = _changes(chunk)
        out["champions"][b.group(1)] = {
            "name": _text(b.group(2)),
            "summary": _text(ctx.group(1)) if ctx else "",
            "changes": changes, "verdict": verdict(changes)}
    mayhem = _section(html, "ARAM: Mayhem")
    details = list(_DETAIL_RE.finditer(mayhem))
    for i, d in enumerate(details):
        if _text(d.group(1)) != "Champions":
            continue
        part = mayhem[d.end():details[i + 1].start() if i + 1 < len(details) else len(mayhem)]
        for e in _ENTRY_RE.finditer(part):
            changes = [_parse_line(li.group(1), "Mayhem")
                       for li in _LI_RE.finditer(e.group(2))]
            if changes:
                out["mayhem"][_norm(_text(e.group(1)))] = {
                    "name": _text(e.group(1)), "changes": changes,
                    "verdict": verdict(changes)}
    return out
