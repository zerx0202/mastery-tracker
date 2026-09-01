#!/usr/bin/env python3
"""
Odpowiedz na pytanie: przepychac cala pule czy dazyc jednym championem?

Symulacja Monte Carlo na prawdziwych danych: rozklad milestone'ow z ostatniego
snapshotu, p_A i p_S z modelu, losowa pula jak w ARAM Mayhem.

Uruchom:  docker compose exec backend python tools/simulate.py
"""

import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, "/code")

from app import db, model

GOAL = 4
RUNS = 3000
MAX_GAMES = 1500

# Ile championow realnie mozesz wybrac w jednej grze.
# W Mayhemie: twoje losowanie + wspolna lawka. Agent raportowal pule 10-11,
# ale polowa to picki druzyny, ktorych nie wezmiesz.
def _pool_size_from_db():
    """Walidacja 1.09: realne finalne pule kartowe to 11-13, nie 7 - stala
    zanizala p trafienia celu i zawyzala projekcje. Czytamy z bazy."""
    try:
        from app import db
        return db.median_final_pool_size()
    except Exception:
        return 11


POOL_SIZE = _pool_size_from_db()


def load_state():
    sid = db.latest_snapshot_id()
    rows = db.snapshot_rows(sid)
    milestones = {r["champion_id"]: r["milestone"] for r in rows}
    rates = model.champion_rates()
    prior = rates["prior"]
    return milestones, rates["champions"], prior


def p_for(cid, milestone, champ_rates, prior):
    """Prawdopodobienstwo przebicia szczebla, ktory stoi przed championem."""
    threshold = "A-" if milestone < 2 else "S-"
    d = (champ_rates.get(cid) or {}).get(threshold)
    if d and d.get("games", 0) > 0:
        return d["shrunk"]
    return prior[threshold]


# ---------- strategie ----------

def pick_closest(pool, ms, rates, prior):
    """Najblizej celu; przy remisie najwyzsze p."""
    return max(pool, key=lambda c: (ms[c], p_for(c, ms[c], rates, prior)))


def pick_best_odds(pool, ms, rates, prior):
    """Najwieksza szansa przebicia biezacego szczebla."""
    return max(pool, key=lambda c: p_for(c, ms[c], rates, prior))


def pick_breadth(pool, ms, rates, prior):
    """Najnizszy milestone - budowanie szerokiej bazy tanimi A-."""
    return min(pool, key=lambda c: (ms[c], -p_for(c, ms[c], rates, prior)))


def pick_expected(pool, ms, rates, prior):
    """Najmniejsza oczekiwana liczba gier do celu dla tego championa."""
    def cost(c):
        total = 0.0
        for m in range(ms[c], GOAL):
            total += 1.0 / max(p_for(c, m, rates, prior), 1e-6)
        return total
    return min(pool, key=cost)


def pick_random(pool, ms, rates, prior):
    return random.choice(pool)


STRATEGIES = {
    "najblizej celu": pick_closest,
    "najwieksza szansa": pick_best_odds,
    "szerokosc (najnizszy ms)": pick_breadth,
    "min. oczekiwanych gier": pick_expected,
    "losowo": pick_random,
}


def simulate(strategy, milestones, rates, prior, pool_size, runs=RUNS):
    champs = list(milestones.keys())
    results = []
    for _ in range(runs):
        ms = dict(milestones)
        if max(ms.values()) >= GOAL:
            results.append(0)
            continue
        for game in range(1, MAX_GAMES + 1):
            pool = random.sample(champs, pool_size)
            pool = [c for c in pool if ms[c] < GOAL]
            if not pool:
                results.append(game)
                break
            c = strategy(pool, ms, rates, prior)
            if random.random() < p_for(c, ms[c], rates, prior):
                ms[c] += 1
                if ms[c] >= GOAL:
                    results.append(game)
                    break
        else:
            results.append(MAX_GAMES)
    return results


def main():
    milestones, rates, prior = load_state()
    dist = Counter(milestones.values())

    print(f"championow: {len(milestones)}")
    print(f"rozklad milestone: {dict(sorted(dist.items()))}")
    print(f"p_A = {prior['A-']}, p_S = {prior['S-']}")
    print(f"pula na gre: {POOL_SIZE} z {len(milestones)}")
    print()

    # ile gier zajmie zobaczenie konkretnego championa
    p_appear = POOL_SIZE / len(milestones)
    print(f"szansa, ze konkretny champion wypadnie: {100*p_appear:.1f}% "
          f"(srednio co {1/p_appear:.0f} gier)")
    print()

    print(f"{'strategia':<26} {'mediana':>8} {'srednia':>8} {'10%':>6} {'90%':>6}")
    print("-" * 58)
    out = {}
    for name, fn in STRATEGIES.items():
        r = sorted(simulate(fn, milestones, rates, prior, POOL_SIZE))
        out[name] = r
        print(f"{name:<26} {statistics.median(r):>8.0f} {statistics.mean(r):>8.0f} "
              f"{r[len(r)//10]:>6.0f} {r[9*len(r)//10]:>6.0f}")

    print()
    best = min(out, key=lambda k: statistics.median(out[k]))
    worst = max(out, key=lambda k: statistics.median(out[k]))
    bm, wm = statistics.median(out[best]), statistics.median(out[worst])
    print(f"najlepsza: {best} ({bm:.0f} gier)")
    print(f"najgorsza: {worst} ({wm:.0f} gier)")
    print(f"roznica: {wm - bm:.0f} gier ({100*(wm/bm - 1):.0f}% wiecej)")

    print()
    print("=== wrazliwosc na wielkosc puli ===")
    for size in (4, 7, 10, 15):
        line = [f"pula {size:>2}:"]
        for name in ("najblizej celu", "min. oczekiwanych gier", "szerokosc (najnizszy ms)"):
            r = simulate(STRATEGIES[name], milestones, rates, prior, size, runs=800)
            line.append(f"{name.split()[0]}={statistics.median(r):.0f}")
        print("  " + "  ".join(line))


if __name__ == "__main__":
    main()
