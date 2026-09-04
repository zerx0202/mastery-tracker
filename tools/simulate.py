#!/usr/bin/env python3
"""
Odpowiedz na pytanie: przepychac cala pule czy dazyc jednym championem?

Symulacja Monte Carlo na prawdziwych danych: rozklad milestone'ow z ostatniego
snapshotu, p_A i p_S z modelu, losowa pula jak w ARAM Mayhem.

Uruchom na KOPII bazy (zasada projektu: analizy nie dotykaja zywego pliku):
  DB_PATH=/sciezka/do/kopii.db python tools/simulate.py
(w kontenerze backendu dziala tez stara komenda, ale czyta zywa baze -
unikac poza szybkim podgladem)
"""

import os
import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, "/code")

from app import db, model, scoring

# Te same zrodla co backend - hardcode GOAL=4 przezyl juz jedna zmiane
# konfiguracji obok siebie
GOAL = int(os.environ.get("GOAL_MILESTONE", "4"))
MODE = os.environ.get("DEFAULT_MODE") or None
RUNS = 3000
MAX_GAMES = 1500

# Ile championow realnie mozesz wybrac w jednej grze.
# W Mayhemie: twoje losowanie + wspolna lawka. Agent raportowal pule 10-11,
# ale polowa to picki druzyny, ktorych nie wezmiesz.
def _pool_size_from_db():
    """Walidacja 1.09: realne finalne pule kartowe to 11-13, nie 7 - stala
    zanizala p trafienia celu i zawyzala projekcje. Czytamy z bazy."""
    try:
        return db.median_final_pool_size()
    except Exception:
        return 11


def load_state():
    """Stan misji + drabinka progow. Champion na GOAL wypada z rosteru
    (jak w mission_projection) - po domknieciu misji kazdy przebieg konczyl
    sie zerem i porownanie strategii padalo ZeroDivisionError."""
    sid = db.latest_snapshot_id()
    if sid is None:
        sys.exit("brak snapshotow w bazie - najpierw POST /api/snapshot")
    rows = db.snapshot_rows(sid)
    milestones = {r["champion_id"]: r["milestone"] for r in rows
                  if r["milestone"] < GOAL}
    if len(milestones) < 2:
        sys.exit("mniej niz 2 championy ponizej celu - nie ma czego symulowac")
    rates = model.champion_rates(MODE)
    return milestones, rates["champions"], rates["prior"], db.get_ladder()


def p_for(cid, milestone, champ_rates, prior, ladder):
    """Prawdopodobienstwo przebicia szczebla - prog z drabinki w BAZIE,
    ta sama sciezka co backend (scoring._p_step). Zaszyty na sztywno
    "A- if ms<2 else S-" rozjechalby sie cicho, gdy Riot zmieni wymogi:
    backend by sie zaadaptowal, narzedzie liczyloby na starej drabince."""
    step = ladder.get(milestone)
    if step is None:
        return 0.05
    p, _th, _n = scoring._p_step(cid, step, champ_rates, prior)
    return p or 0.05


def need_for(milestone, ladder):
    """Krotnosc szczebla (bonus milestone IV->5: S- x2) - ta sama sciezka
    co scoring._rung; nieznany szczebel = 1."""
    return scoring._rung(ladder.get(milestone))[1]


# ---------- strategie ----------
# have = oceny juz uzbierane na biezacym szczeblu (symulacja je prowadzi);
# uzywa go tylko strategia licząca koszt, reszta patrzy na milestone i p.

def pick_closest(pool, ms, rates, prior, ladder, have=None):
    """Najblizej celu; przy remisie najwyzsze p."""
    return max(pool, key=lambda c: (ms[c], p_for(c, ms[c], rates, prior, ladder)))


def pick_best_odds(pool, ms, rates, prior, ladder, have=None):
    """Najwieksza szansa przebicia biezacego szczebla."""
    return max(pool, key=lambda c: p_for(c, ms[c], rates, prior, ladder))


def pick_breadth(pool, ms, rates, prior, ladder, have=None):
    """Najnizszy milestone - budowanie szerokiej bazy tanimi A-."""
    return min(pool, key=lambda c: (ms[c], -p_for(c, ms[c], rates, prior, ladder)))


def pick_expected(pool, ms, rates, prior, ladder, have=None):
    """Najmniejsza oczekiwana liczba gier do celu dla tego championa
    (brakujace oceny / p na kazdym szczeblu, jak scoring.expected_games)."""
    def cost(c):
        total = 0.0
        for m in range(ms[c], GOAL):
            need = need_for(m, ladder)
            left = max(1, need - (have or {}).get(c, 0)) if m == ms[c] else need
            total += left / max(p_for(c, m, rates, prior, ladder), 1e-6)
        return total
    return min(pool, key=cost)


def pick_random(pool, ms, rates, prior, ladder, have=None):
    return random.choice(pool)


STRATEGIES = {
    "najblizej celu": pick_closest,
    "najwieksza szansa": pick_best_odds,
    "szerokosc (najnizszy ms)": pick_breadth,
    "min. oczekiwanych gier": pick_expected,
    "losowo": pick_random,
}


def sample_weighted(champs, weights, k, rnd=random):
    """Losowanie puli bez zwracania, wazone popularnoscia - do sondy
    ponizej. Odrzucanie duplikatow zamiast algorytmu bez zwracania:
    przy k=11 z ~170 championow narzut jest pomijalny."""
    pool, seen = [], set()
    while len(pool) < k and len(seen) < len(champs):
        c = rnd.choices(champs, weights=weights, k=1)[0]
        if c not in seen:
            seen.add(c)
            pool.append(c)
    return pool


def simulate(strategy, milestones, rates, prior, pool_size, ladder,
             runs=RUNS, sampler=None):
    champs = list(milestones.keys())
    results = []
    for _ in range(runs):
        ms = dict(milestones)
        have = dict.fromkeys(champs, 0)     # oceny uzbierane na biezacym szczeblu
        for game in range(1, MAX_GAMES + 1):
            pool = (sampler(champs, pool_size) if sampler
                    else random.sample(champs, min(pool_size, len(champs))))
            pool = [c for c in pool if ms[c] < GOAL]
            if not pool:
                continue
            c = strategy(pool, ms, rates, prior, ladder, have)
            if random.random() < p_for(c, ms[c], rates, prior, ladder):
                have[c] += 1
                if have[c] < need_for(ms[c], ladder):
                    continue
                have[c] = 0
                ms[c] += 1
                if ms[c] >= GOAL:
                    results.append(game)
                    break
        else:
            results.append(MAX_GAMES)
    return results


def main():
    milestones, rates, prior, ladder = load_state()
    pool_size = _pool_size_from_db()
    dist = Counter(milestones.values())

    print(f"championow ponizej celu (GOAL={GOAL}, tryb={MODE or 'wszystkie'}): "
          f"{len(milestones)}")
    print(f"rozklad milestone: {dict(sorted(dist.items()))}")
    print(f"p_A = {prior['A-']}, p_S = {prior['S-']}")
    print(f"pula na gre: {pool_size} z {len(milestones)}")
    print()

    # ile gier zajmie zobaczenie konkretnego championa
    p_appear = pool_size / len(milestones)
    print(f"szansa, ze konkretny champion wypadnie: {100*p_appear:.1f}% "
          f"(srednio co {1/p_appear:.0f} gier)")
    print()

    print(f"{'strategia':<26} {'mediana':>8} {'srednia':>8} {'10%':>6} {'90%':>6}")
    print("-" * 58)
    out = {}
    for name, fn in STRATEGIES.items():
        r = sorted(simulate(fn, milestones, rates, prior, pool_size, ladder))
        out[name] = r
        print(f"{name:<26} {statistics.median(r):>8.0f} {statistics.mean(r):>8.0f} "
              f"{r[len(r)//10]:>6.0f} {r[9*len(r)//10]:>6.0f}")

    print()
    best = min(out, key=lambda k: statistics.median(out[k]))
    worst = max(out, key=lambda k: statistics.median(out[k]))
    bm, wm = statistics.median(out[best]), statistics.median(out[worst])
    print(f"najlepsza: {best} ({bm:.0f} gier)")
    print(f"najgorsza: {worst} ({wm:.0f} gier)")
    ratio = f" ({100*(wm/bm - 1):.0f}% wiecej)" if bm else ""
    print(f"roznica: {wm - bm:.0f} gier{ratio}")

    # (W6/p16, przeglad 2.09) Jednorazowa sonda: projekcja losuje pule
    # JEDNOSTAJNIE, a realna pula jest wazona popularnoscia - ktora
    # koreluje z progiem oceny (rho=-0.583, karta 46). Jesli mediana
    # rusza sie <10%, temat zamykamy na stale.
    print()
    print("=== sonda: pula jednostajna vs wazona popularnoscia ===")
    pop = db.champion_sb_popularity()
    w_of = {c: max(pop.get(c, 0), 1) for c in milestones}  # min 1: nikt nie znika

    def weighted(cs, k):
        return sample_weighted(cs, [w_of[c] for c in cs], k)

    for name in ("min. oczekiwanych gier", "najblizej celu"):
        u = statistics.median(simulate(STRATEGIES[name], milestones, rates,
                                       prior, pool_size, ladder, runs=800))
        w = statistics.median(simulate(STRATEGIES[name], milestones, rates,
                                       prior, pool_size, ladder, runs=800,
                                       sampler=weighted))
        delta = 100 * (w - u) / u if u else 0.0
        verdict = "szum, zamknac temat" if abs(delta) < 10 else "ROZNICA REALNA"
        print(f"  {name:<26} jednostajna={u:.0f}  wazona={w:.0f}  "
              f"delta={delta:+.0f}%  [{verdict}]")

    print()
    print("=== wrazliwosc na wielkosc puli ===")
    for size in (4, 7, 10, 15):
        line = [f"pula {size:>2}:"]
        for name in ("najblizej celu", "min. oczekiwanych gier", "szerokosc (najnizszy ms)"):
            r = simulate(STRATEGIES[name], milestones, rates, prior, size,
                         ladder, runs=800)
            line.append(f"{name.split()[0]}={statistics.median(r):.0f}")
        print("  " + "  ".join(line))


if __name__ == "__main__":
    main()
