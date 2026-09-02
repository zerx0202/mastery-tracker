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
import re
import time

from . import db

BALANCE_URL = "https://arammayhem.com/aram-balance/"

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
            out[name_m.group(1).strip()] = mods
    return out


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


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

    payload = {"fetched_at": ts, "source": BALANCE_URL,
               "count": len(champions), "unmatched": unmatched,
               "champions": champions}
    db.set_json_setting("mayhem_balance", payload)
    db.log_event("balance_refresh", {"count": len(champions),
                                     "unmatched": len(unmatched)}, ts)
    return {"stored": True, "parsed": len(parsed), "matched": len(champions),
            "unmatched": unmatched}
