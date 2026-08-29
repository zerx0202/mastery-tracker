"""
Ranking championow: oczekiwana liczba gier do ukonczenia misji.

Zamiast sumy wazonej arbitralnych podwynikow liczymy jedna wielkosc
o jasnym znaczeniu: ile gier tym championem srednio zajmie dojscie
do GOAL_MILESTONE.

    E(c) = suma po szczeblach m od obecnego do celu z 1 / p(c, m)

gdzie p(c, m) to prawdopodobienstwo przebicia szczebla m na championie c,
brane z modelu (grade_observation -> regresja) i sciagane do sredniej
globalnej proporcjonalnie do liczby gier.

Dlaczego tak, a nie inaczej: symulacja Monte Carlo na prawdziwym stanie
(175 championow, p_A=0.54, p_S=0.06, losowa pula 7 na gre) dala mediane
84 gier dla strategii "najblizej celu" i "min. oczekiwanych gier", 632 dla
"najwieksza szansa przebicia" i brak ukonczenia dla strategii szerokosci.
Misja wymaga dwoch S- na TYM SAMYM championie, wiec kazda strategia
unikajaca prob S- nigdy nie konczy.
"""

import math
import time

from .db import GRADE_RANK

# Ponizej tylu gier na championie prawdopodobienstwo jest praktycznie
# rowne sredniej globalnej - oznaczamy to jako niska pewnosc.
LOW_CONFIDENCE_GAMES = 8      # ponizej tego jedna gra przesadza o wyniku

# Kara za nieznany szczebel drabinki (nie powinno sie zdarzac,
# odkad learn_ladder zna komplet)
UNKNOWN_STEP_COST = 20.0


def cheapest_grade(req):
    if not req:
        return None
    return min(req.keys(), key=lambda g: GRADE_RANK.get(g, 99))


def _threshold_for(step):
    """Ktory z dwoch modeli progowych obsluguje ten szczebel."""
    g = cheapest_grade((step or {}).get("require_grades"))
    if not g:
        return None
    return "S-" if GRADE_RANK.get(g, 0) >= GRADE_RANK["S-"] else "A-"


def _p_step(champion_id, step, rates, prior):
    """Prawdopodobienstwo przebicia jednego szczebla."""
    th = _threshold_for(step)
    if th is None:
        return None, None, 0
    d = (rates.get(champion_id) or {}).get(th) or {}
    games = d.get("games", 0)
    if games:
        return d.get("shrunk") or prior.get(th, 0.2), th, games
    return prior.get(th, 0.2), th, 0


def expected_games(champion_id, milestone, goal, ladder, rates, prior):
    """Rozklada droge do celu na szczeble i sumuje 1/p dla kazdego."""
    total = 0.0
    steps = []
    known = True
    for m in range(milestone, goal):
        step = ladder.get(m)
        if step is None:
            known = False
            total += UNKNOWN_STEP_COST
            steps.append({"from": m, "to": m + 1, "grade": None,
                          "p": None, "games": UNKNOWN_STEP_COST, "known": False})
            continue
        p, th, n = _p_step(champion_id, step, rates, prior)
        cost = 1.0 / max(p, 1e-6)
        total += cost
        steps.append({
            "from": m, "to": m + 1,
            "grade": cheapest_grade(step["require_grades"]),
            "threshold": th,
            "p": round(p, 4),
            "games": round(cost, 1),
            "own_games": n,
            "known": True,
        })
    return total, steps, known


def expected_games_prior_only(milestone, goal, ladder, prior):
    """Wariant ostrozny: ignoruje wlasne wyniki na championie, liczy tylko
    ze srednich. Pokazuje, ile bylby wart champion, gdyby nie ta garstka gier."""
    total = 0.0
    for m in range(milestone, goal):
        step = ladder.get(m)
        if step is None:
            total += UNKNOWN_STEP_COST
            continue
        th = _threshold_for(step)
        total += 1.0 / max(prior.get(th, 0.2), 1e-6)
    return total


def score_rows(rows, ladder, rates, prior, goal, now_s=None):
    """Modyfikuje rows w miejscu. Sortuje rosnaco po oczekiwanej liczbie gier."""
    now_s = now_s or int(time.time())

    for r in rows:
        cid = r["champion_id"]
        exp, steps, known = expected_games(cid, r["milestone"], goal, ladder, rates, prior)

        r["expected_games"] = round(exp, 1)
        r["path"] = steps
        r["path_known"] = known
        r["steps_remaining"] = max(0, goal - r["milestone"])

        # najblizszy szczebel - to widzisz w champ selekcie
        nxt = steps[0] if steps else None
        r["next_grade"] = (nxt or {}).get("grade")
        r["next_p"] = (nxt or {}).get("p")
        r["next_threshold"] = (nxt or {}).get("threshold")

        own = sum(s.get("own_games", 0) for s in steps)
        r["own_games_on_champ"] = own
        r["confidence"] = "niska" if own < LOW_CONFIDENCE_GAMES else "ok"

        # ile wyszloby bez wlasnych wynikow - miara tego, jak bardzo
        # optymistyczna ocena opiera sie na garstce gier
        cons = expected_games_prior_only(r["milestone"], goal, ladder, prior)
        r["expected_games_conservative"] = round(cons, 1)
        r["optimism"] = round(cons / exp, 2) if exp > 0 else None

        # wynik 0-100 wylacznie do paska w UI - sortuje expected_games
        r["score"] = round(100.0 * math.exp(-exp / 40.0), 1)

    rows.sort(key=lambda x: (x["expected_games"], -x["points"]))
    return rows


def summarize(rows, goal):
    """Krotkie podsumowanie: ile gier do misji przy obecnym stanie."""
    if not rows:
        return None
    best = rows[0]
    return {
        "goal": goal,
        "best_champion": best.get("name"),
        "best_champion_id": best.get("champion_id"),
        "expected_games": best.get("expected_games"),
        "next_grade": best.get("next_grade"),
        "confidence": best.get("confidence"),
        "expected_games_conservative": best.get("expected_games_conservative"),
        "own_games": best.get("own_games_on_champ"),
        "candidates_within_2x": sum(
            1 for r in rows if r["expected_games"] <= 2 * best["expected_games"]),
        "warning": (
            f"ocena opiera sie na {best.get('own_games_on_champ')} grach - "
            f"bez nich wyszloby {best.get('expected_games_conservative')} gier"
            if best.get("confidence") == "niska" else None),
    }
