"""(6) Slownik augmentow Mayhema z binu gry na CommunityDragonie.

Zrodlo: game/maps/modespecificdata/kiwi.bin.json - 222 wpisy AugmentData
z AugmentPlatformId zgodnym z id w naszych blokach eog (walidacja C6 na
eksporcie produkcji: pokrycie 57/57). Kuratorowanego slownika dla KIWI
nie ma (cdragon/ zna tylko arena/ i tft/), a slownik Areny to rozlaczna
przestrzen id (sonda P9: 0/57). Wariant KIWI_JADE (custom classic,
klaster id 7001+) ignorujemy - id spoza slownika dostaje name=None.

Nazwa wyswietlana = humanizowany AugmentNameId (ARAM_TransmutePrismatic
-> "Transmute Prismatic"). Pelne nazwy lokalizacyjne siedza w
lol.stringtable.json (32 MB) - swiadomie pomijane, NameId wystarcza.

DECYZJA (ta sama co przy balansie i sciadze): zrodlo zewnetrzne sluzy
WYLACZNIE do etykiet/wyswietlania - nigdy jako cecha modelu ani
normalizator.
"""
import json
import re
import time

from . import db

BIN_URL = ("https://raw.communitydragon.org/latest/game/maps/"
           "modespecificdata/kiwi.bin.json")

# Ponizej tylu wpisow uznajemy plik za przebudowany/uszkodzony i NIE
# nadpisujemy ostatnich dobrych danych (zywy plik: 222; wzorzec balance.py).
MIN_AUGMENTS = 100

# rarity jak w Arenie; brak pola w binie (62/222 wpisow) = Silver
RARITY = {0: "Silver", 1: "Gold", 2: "Prismatic"}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanize(name_id):
    """ARAM_TransmutePrismatic -> Transmute Prismatic. Prefiks ARAM_ to
    czysty szum przestrzeni nazw; Upgrade_ niesie znaczenie i zostaje."""
    s = (name_id or "").strip()
    if s.startswith("ARAM_"):
        s = s[len("ARAM_"):]
    return " ".join(_CAMEL_RE.sub(" ", p) for p in s.split("_") if p)


def parse_augments(text):
    """JSON binu -> {platform_id: {name, name_id, rarity}}. Bin to slownik
    sciezka->obiekt; interesuja nas wylacznie __type == AugmentData."""
    data = json.loads(text)
    objs = data.values() if isinstance(data, dict) else data
    out = {}
    for o in objs:
        if not isinstance(o, dict) or o.get("__type") != "AugmentData":
            continue
        pid = o.get("AugmentPlatformId")
        if pid is None:
            continue
        nid = o.get("AugmentNameId") or ""
        out[int(pid)] = {"name": humanize(nid), "name_id": nid,
                         "rarity": int(o.get("rarity") or 0)}
    return out


def store_augments(text, patch=None, ts=None):
    """Parsuje i zapisuje slownik w settings. Przy podejrzanie malym
    wyniku zostawia stare dane - przebudowa binu nie moze zjesc etykiet."""
    ts = ts or int(time.time())
    try:
        parsed = parse_augments(text)
    except ValueError:
        parsed = {}
    if len(parsed) < MIN_AUGMENTS:
        db.log_event("augments_parse_failed", {"parsed": len(parsed)}, ts)
        return {"stored": False, "parsed": len(parsed),
                "reason": f"mniej niz {MIN_AUGMENTS} augmentow"}
    db.set_json_setting("augment_book", {
        "fetched_at": ts, "patch": patch, "source": BIN_URL,
        "count": len(parsed),
        "augments": {str(k): v for k, v in parsed.items()}})
    db.log_event("augments_refresh", {"count": len(parsed), "patch": patch}, ts)
    return {"stored": True, "count": len(parsed), "parsed": len(parsed)}


def get_book():
    return db.get_json_setting("augment_book") or {}


def names_for(ids, book=None):
    """Lista id z eog -> etykiety. Nieznane id (np. klaster 7001+ wariantu
    JADE albo nowy patch przed refreshem) dostaja name=None - UI pokazuje
    wtedy surowe id zamiast zgadywac. book podaje caller, gdy woła w petli
    (zeby nie czytac settings per wiersz)."""
    if book is None:
        book = get_book().get("augments") or {}
    out = []
    for i in ids or []:
        ent = book.get(str(i))
        out.append({"id": i,
                    "name": ent["name"] if ent else None,
                    "rarity": ent.get("rarity") if ent else None})
    return out
