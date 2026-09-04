"""
Ranking championow: oczekiwana liczba gier do ukonczenia misji.

Zamiast sumy wazonej arbitralnych podwynikow liczymy jedna wielkosc
o jasnym znaczeniu: ile gier tym championem srednio zajmie dojscie
do GOAL_MILESTONE.

    E(c) = suma po szczeblach m od obecnego do celu z k(m) / p(c, m)

gdzie p(c, m) to prawdopodobienstwo przebicia szczebla m na championie c,
brane z modelu (grade_observation -> regresja) i sciagane do sredniej
globalnej proporcjonalnie do liczby gier, a k(m) to liczba ocen, ktorych
na szczeblu jeszcze brakuje (IV->5, bonus milestone: S- x2; oceny juz
uzbierane na biezacym szczeblu odejmujemy).

Dlaczego tak, a nie inaczej: symulacja Monte Carlo na prawdziwym stanie
(175 championow, p_A=0.54, p_S=0.06, losowa pula 7 na gre) dala mediane
84 gier dla strategii "najblizej celu" i "min. oczekiwanych gier", 632 dla
"najwieksza szansa przebicia" i brak ukonczenia dla strategii szerokosci.
Misja wymaga dwoch S- na TYM SAMYM championie, wiec kazda strategia
unikajaca prob S- nigdy nie konczy.

Aktualizacja 1.09: powyzsza symulacja byla liczona dla puli 7; realne pule
systemu kart to 11-13 (mediana finalnych pul z bazy) i tools/simulate.py
czyta ja teraz z danych. Ranking strategii bez zmian, bezwzgledne mediany
gier sa nizsze.
"""

import math

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


def _rung(step):
    """Prog i krotnosc szczebla: {'S-': 2} = dwie oceny >= S-. Drabinka
    Riota trzyma jeden klucz na szczebel; gdyby pojawily sie dwa, liczymy
    najtanszy (jak cheapest_grade) - jawne uproszczenie."""
    req = (step or {}).get("require_grades") or {}
    grade = cheapest_grade(req)
    need = int(req.get(grade) or 1) if grade else 1
    return grade, max(1, need)


def _have(grades_earned, grade):
    """Ile ocen biezacego szczebla juz spelnia prog. milestoneGrades z LCU
    trzyma WSZYSTKIE oceny szczebla, takze ponizej progu (sonda C2:
    ['B+', 'B+'] przy wymogu A- x1)."""
    if not grade:
        return 0
    lo = GRADE_RANK.get(grade, 99)
    return sum(1 for g in grades_earned or [] if GRADE_RANK.get(g, -1) >= lo)


def expected_games(champion_id, milestone, goal, ladder, rates, prior,
                   grades_earned=None):
    """Rozklada droge do celu na szczeble i sumuje (brakujace oceny)/p.

    Oczekiwana liczba prob do k sukcesow to k/p, wiec krotnosc szczebla
    wchodzi wprost do kosztu. Na biezacym szczeblu odejmujemy oceny juz
    uzbierane, z podloga 1 gry: nadwyzka bez awansu to opoznienie
    snapshotu, nie darmowy szczebel. Do 3.09 koszt byl 1/p niezaleznie
    od krotnosci - przy celu 4 bez skutkow (wszystkie szczeble x1), przy
    celu 5 (bonus milestone, S- x2) zanizal ostatni szczebel dwukrotnie."""
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
        grade, need = _rung(step)
        have = _have(grades_earned, grade) if m == milestone else 0
        remaining = max(1, need - have)
        p, th, n = _p_step(champion_id, step, rates, prior)
        cost = remaining / max(p or 0.0, 1e-6)
        total += cost
        steps.append({
            "from": m, "to": m + 1,
            "grade": grade,
            "threshold": th,
            "p": round(p, 4) if p is not None else None,
            "need": need, "have": have, "remaining": remaining,
            "games": round(cost, 1),
            "own_games": n,
            "known": True,
        })
    return total, steps, known


def expected_games_prior_only(milestone, goal, ladder, prior, grades_earned=None):
    """Wariant ostrozny: ignoruje wlasne wyniki na championie, liczy tylko
    ze srednich. Pokazuje, ile bylby wart champion, gdyby nie ta garstka gier.
    Krotnosc szczebla i uzbierane oceny jak w expected_games."""
    total = 0.0
    for m in range(milestone, goal):
        step = ladder.get(m)
        if step is None:
            total += UNKNOWN_STEP_COST
            continue
        grade, need = _rung(step)
        have = _have(grades_earned, grade) if m == milestone else 0
        th = _threshold_for(step)
        total += max(1, need - have) / max(prior.get(th, 0.2), 1e-6)
    return total


def score_rows(rows, ladder, rates, prior, goal):
    """Modyfikuje rows w miejscu. Sortuje rosnaco po oczekiwanej liczbie gier."""
    for r in rows:
        cid = r["champion_id"]
        earned = r.get("grades_earned")
        exp, steps, known = expected_games(cid, r["milestone"], goal, ladder,
                                           rates, prior, earned)

        r["expected_games"] = round(exp, 1)
        r["path"] = steps
        r["path_known"] = known
        r["steps_remaining"] = max(0, goal - r["milestone"])

        # najblizszy szczebel - to widzisz w champ selekcie; krotnosc
        # i uzbierane oceny ida do szyny ("S- x2, masz 1")
        nxt = steps[0] if steps else None
        r["next_grade"] = (nxt or {}).get("grade")
        r["next_p"] = (nxt or {}).get("p")
        r["next_threshold"] = (nxt or {}).get("threshold")
        r["next_need"] = (nxt or {}).get("need")
        r["next_have"] = (nxt or {}).get("have")

        own = sum(s.get("own_games", 0) for s in steps)
        r["own_games_on_champ"] = own
        r["confidence"] = "niska" if own < LOW_CONFIDENCE_GAMES else "ok"

        # ile wyszloby bez wlasnych wynikow - miara tego, jak bardzo
        # optymistyczna ocena opiera sie na garstce gier
        cons = expected_games_prior_only(r["milestone"], goal, ladder, prior, earned)
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
