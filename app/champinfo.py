"""(J) Sciaga o championie z OFICJALNYCH danych - odpowiedz na uwage 3.09
("cos w stylu Blitza, ale czytelne w grze") po tym, jak zabraklo zrodla
tekstu: arammayhem nie ma tekstu, wiki oddaje serwerom 403, Fandom martwy
(404) - sonda 4.09.

Zrodla (obie 200 z golym UA backendu):
  - Data Dragon data/pl_PL/champion/<Key>.json: allytips (jak grac ta
    postacia = MOCNE strony) i enemytips (jak grac PRZECIW = SLABE strony),
    po polsku, plus tags i info (attack/defense/magic/difficulty 0-10);
  - CommunityDragon .../v1/champions/<id>.json: playstyleInfo (damage,
    durability, crowdControl, mobility, utility w skali 1-3) i tacticalInfo
    (style 0-10: 0 = autoataki, 10 = umiejetnosci; difficulty 1-3;
    damageType; attackType).
Porady Riota sa krotkie i stabilne (zmieniaja sie tylko przy reworku) - to
nie analiza meta, tylko "czym ta postac jest" na trzy sekundy w champ
selekcie. Cache per patch razem z reszta sciagi (endpoint /cheatsheet).

DECYZJA (jak przy balansie, sciadze i notkach): zrodlo zewnetrzne sluzy
WYLACZNIE do wyswietlania - nigdy jako cecha modelu ani normalizator.
"""

DD_CHAMPION_URL = ("https://ddragon.leagueoflegends.com/cdn/{version}/data/"
                   "pl_PL/champion/{key}.json")
CDRAGON_CHAMPION_URL = ("https://raw.communitydragon.org/latest/plugins/"
                        "rcp-be-lol-game-data/global/default/v1/champions/{id}.json")
DAMAGE_PL = {"kPhysical": "fizyczne", "kMagic": "magiczne", "kMixed": "mieszane"}
PLAYSTYLE_KEYS = ("damage", "durability", "crowdControl", "mobility", "utility")


def parse_dd(data, key):
    """JSON Data Dragona (champion/<Key>.json) -> porady i tagi. Puste {}
    = brak championa w pliku (zly klucz, przebudowa) - caller nie oznacza
    tego jako sukces."""
    ch = ((data or {}).get("data") or {}).get(key) or {}
    if not ch:
        return {}
    tips = lambda k: [t.strip() for t in ch.get(k) or [] if str(t).strip()][:3]  # noqa: E731
    return {"tips_ally": tips("allytips"), "tips_enemy": tips("enemytips"),
            "tags": list(ch.get("tags") or []), "info": dict(ch.get("info") or {})}


def parse_cdragon(data):
    """JSON CDragona (champions/<id>.json) -> profil stylu gry. Puste {}
    = brak obu sekcji."""
    ps = (data or {}).get("playstyleInfo") or {}
    ti = (data or {}).get("tacticalInfo") or {}
    if not ps and not ti:
        return {}
    out = {}
    if ps:
        out["playstyle"] = {k: ps.get(k) for k in PLAYSTYLE_KEYS if ps.get(k) is not None}
    if ti:
        out["tactical"] = {
            "style": ti.get("style"), "difficulty": ti.get("difficulty"),
            "damage": DAMAGE_PL.get(ti.get("damageType"), ti.get("damageType")),
            "ranged": ti.get("attackType") == "ranged"}
    return out
