"""(48) Mnozniki balansu Mayhema per champion ze strony zbiorczej
arammayhem.com/aram-balance/ (jedyne znalezione zrodlo prowadzace balans
TEGO trybu: wiki tabelaryzuje tylko zwykly ARAM, a tryby maja rozny balans
- potwierdzone custom testem 1.09).

DECYZJA (2.09): zrodlo zewnetrzne sluzy WYLACZNIE do wyswietlania
("ten champion jest sciety w Mayhemie") - nigdy jako cecha modelu ani
normalizator; tamta decyzja z CLAUDE.md stoi nietknieta.

Strona jest statycznym HTML (Astro): wiersz championa to
<a ... class="champion-row ..." data-name="slug"> z <div
class="font-medium">Nazwa</div> i spanami "Etykieta: +5%" / "-10" (haste
bez procentu). Champion bez modyfikatorow nie wystepuje na liscie.
"""
# alias: parametry funkcji nazywaja sie `html` i przeslonilyby modul
import html as _html
import json
import re
import time

from . import db

BALANCE_URL = "https://arammayhem.com/aram-balance/"

# (49) Strona buildu championa - slug to klucz Data Dragona lowercase
# (zweryfikowane: monkeyking 200, wukong 404; nunu, drmundo, kogmaw 200).
BUILD_URL = "https://arammayhem.com/build/{slug}/"

# Ponizej tylu sparsowanych championow uznajemy fetch za uszkodzony
# (przebudowa strony, blokada) i NIE nadpisujemy ostatnich dobrych danych.
MIN_CHAMPIONS = 20

_NAME_RE = re.compile(r'class="font-medium">([^<]+)<')
_MOD_RE = re.compile(r">([A-Z][A-Za-z ]+): ([+-][\d.]+%?)<")


def parse_balance(html):
    """HTML -> {nazwa championa: {etykieta: wartosc-string}}. Wartosci
    zostaja stringami ("+5%", "-10") - front koloruje po znaku, a jednostka
    (procent vs plaski haste) jest czescia informacji."""
    out = {}
    for chunk in html.split('class="champion-row')[1:]:
        name_m = _NAME_RE.search(chunk)
        if not name_m:
            continue
        mods = dict(_MOD_RE.findall(chunk))
        if mods:
            # strona escapuje encje (Kog&#39;Maw, Nunu &amp; Willump) -
            # bez unescape trzy championy nie przypinaly sie do id
            out[_html.unescape(name_m.group(1)).strip()] = mods
    return out


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


# ---------- (49) sciaga-z-danych per champion ----------
#
# Zamiast tekstu mocnych/slabych stron (nie istnieje w zadnym zrodle)
# strona buildu daje dane: tier + winrate (meta description), ranking
# augmentow (JSON-LD ItemList - warstwa semantyczna, stabilniejsza niz
# tailwindowy markup) i sekwencje skilli (tekst "Skill Sequence: ...").
# Ta sama decyzja graniczna co przy mnoznikach: WYLACZNIE wyswietlanie.

_DESC_RE = re.compile(
    r'name="description" content="[^"]*?Patch ([\d.]+):[^"]*?'
    r'([A-Z][+-]?) tier with ([\d.]+)% win rate')
_LD_AUG_RE = re.compile(
    r'<script type="application/ld\+json">'
    r'(\{"@context":"https://schema\.org","@type":"ItemList",'
    r'"name":"Best Augments for [^<]*?\})</script>')
_SKILL_RE = re.compile(r"Skill Sequence: ((?:[QWER] )+[QWER])")


def parse_build(html):
    """HTML strony buildu -> {tier, win_rate, site_patch, augments,
    skill_sequence, skill_priority}. Puste {} = strona nie do odczytania
    (przebudowa) - caller trzyma to jako nieudany fetch, nie sciagawke."""
    out = {}
    m = _DESC_RE.search(html)
    if m:
        out["site_patch"] = m.group(1)
        out["tier"] = m.group(2)
        out["win_rate"] = float(m.group(3))
    m = _LD_AUG_RE.search(html)
    if m:
        try:
            items = json.loads(m.group(1)).get("itemListElement") or []
            names = [i.get("name") for i in items if i.get("name")]
            if names:
                out["augments"] = names[:10]
        except ValueError:
            pass
    m = _SKILL_RE.search(html)
    if m:
        seq = m.group(1).split()
        out["skill_sequence"] = " ".join(seq)
        basics = [s for s in "QWE" if s in seq]
        # priorytet maksowania: liczba punktow malejaco, remis = wczesniejszy
        basics.sort(key=lambda s: (-seq.count(s), seq.index(s)))
        out["skill_priority"] = " > ".join(basics)
    return out


def store_balance(html, ts=None):
    """Parsuje i zapisuje mnozniki pod id championow z bazy. Zwraca
    podsumowanie; przy podejrzanie malym wyniku zostawia stare dane."""
    ts = ts or int(time.time())
    parsed = parse_balance(html)
    if len(parsed) < MIN_CHAMPIONS:
        db.log_event("balance_parse_failed", {"parsed": len(parsed)}, ts)
        return {"stored": False, "parsed": len(parsed),
                "reason": f"mniej niz {MIN_CHAMPIONS} championow"}

    with db.connect() as con:
        by_name = {}
        for r in con.execute("SELECT id, name, key FROM champion"):
            by_name[_norm(r["name"])] = r["id"]
            by_name[_norm(r["key"])] = r["id"]

    champions, unmatched = {}, []
    for name, mods in parsed.items():
        cid = by_name.get(_norm(name))
        if cid is None:
            unmatched.append(name)
            continue
        champions[str(cid)] = mods

    if len(champions) < MIN_CHAMPIONS:
        # bramka wyzej chroni przed zepsutym PARSEREM, ale nie przed
        # zepsutym DOPASOWANIEM (precedens: encje HTML, 2.09) - przy
        # parsed=103 i matched=0 stare, dobre mnozniki bylyby nadpisane
        # pustka ze stored=True
        db.log_event("balance_match_failed",
                     {"parsed": len(parsed), "matched": len(champions)}, ts)
        return {"stored": False, "parsed": len(parsed),
                "matched": len(champions), "unmatched": unmatched[:10],
                "reason": f"dopasowano mniej niz {MIN_CHAMPIONS} championow"}

    payload = {"fetched_at": ts, "source": BALANCE_URL,
               "count": len(champions), "unmatched": unmatched,
               "champions": champions}
    db.set_json_setting("mayhem_balance", payload)
    db.log_event("balance_refresh", {"count": len(champions),
                                     "unmatched": len(unmatched)}, ts)
    return {"stored": True, "parsed": len(parsed), "matched": len(champions),
            "unmatched": unmatched}
