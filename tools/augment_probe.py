#!/usr/bin/env python3
"""Sonda pokrycia id augmentow (raport 2.09, P9 etap 1) - OFFLINE, na kopii.

KOREKTA czlowieka 2.09: augmenty Areny ROZNIA sie od Mayhemowych, wiec to
jest wylacznie rozpoznanie, czy przestrzen id w ogole sie pokrywa
(oczekiwanie: niskie) - NIE zrodlo slownika. Droga wlasciwa to biny gry
(gameplay.kiwi*.bin.json, a la Lanternko) - etap 2, osobna decyzja.

Uzycie (czlowiek; zywej bazy nie dotykamy, patrz CLAUDE.md):
  curl -s https://<node>.tailnet.ts.net/api/export -H "X-API-Token: ..." > export.json
  python tools/augment_probe.py export.json
"""
import json
import sys
import urllib.request

ARENA_URL = "https://raw.communitydragon.org/latest/cdragon/arena/en_us.json"


def coverage(export, arena):
    """Czyste liczenie - testowalne bez sieci. Zwraca (ids, hit, miss)."""
    ids = set()
    for row in export.get("eog_raw") or []:
        for a in json.loads(row.get("augments") or "[]"):
            try:
                ids.add(int(a))
            except (TypeError, ValueError):
                continue
    by_id = {a.get("id"): a for a in (arena.get("augments") or [])}
    hit = sorted(i for i in ids if i in by_id)
    miss = sorted(i for i in ids if i not in by_id)
    return ids, hit, miss, by_id


def main(path):
    export = json.loads(open(path, encoding="utf-8").read())
    arena = json.loads(urllib.request.urlopen(ARENA_URL, timeout=30).read())
    ids, hit, miss, by_id = coverage(export, arena)
    if not ids:
        print("brak augmentow w eksporcie (kolumna eog_raw.augments)")
        return
    print(f"id augmentow u nas: {len(ids)} | trafienia w slownik Areny: "
          f"{len(hit)} | poza slownikiem: {len(miss)}")
    for i in hit:
        a = by_id[i]
        print(f"  {i}: {a.get('name')} (rarity {a.get('rarity')})")
    if miss:
        print("poza slownikiem Areny (czysto Mayhemowe?):",
              ", ".join(map(str, miss[:60])))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
