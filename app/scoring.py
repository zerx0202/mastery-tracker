"""Dwupoziomowy ranking championow.

Poziom 1 (twardy): ile szczebli milestone zostalo do celu. Nie da sie ich
przeskoczyc, wiec to porzadek nadrzedny - nie skladnik sumy wazonej.

Poziom 2 (wynik 0..100): jak prawdopodobne, ze dowieziesz NASTEPNY szczebel.
Wszystkie podwyniki sa normalizowane w obrebie puli, zeby wagi mialy
porownywalny wplyw. Brak danych = 0.5 (neutralnie), nie pominiecie -
inaczej kiepska ocena bilaby brak oceny.
"""

import math

from .db import GRADE_RANK

MAX_RANK = GRADE_RANK["S+"]

DEFAULT_WEIGHTS = {
    "grade_req":  2.0,   # jak lekki wymog na nastepny szczebel
    "grade_hist": 2.5,   # jak blisko progu byly Twoje oceny
    "winrate":    1.5,   # WR w trybie, sciagniety do sredniej
    "recency":    1.0,   # swiezosc
    "ppg":        1.0,   # punkty maestrii na gre
    "experience": 0.8,   # ogolne obycie (log punktow)
}

SHRINK_K = 5.0
RECENCY_HALFLIFE = 30.0
NEUTRAL = 0.5


def _shrunk_winrate(wins, games, prior):
    if not games:
        return None
    return (wins + SHRINK_K * prior) / (games + SHRINK_K)


def _grade_hist(grades, required):
    """Najlepsza zebrana ocena wzgledem wymaganej. Brak ocen -> None."""
    if not grades or not required:
        return None
    req = GRADE_RANK.get(required)
    if not req:
        return None
    best = max((GRADE_RANK[g] for g in grades if g in GRADE_RANK), default=None)
    if best is None:
        return None
    return min(1.0, best / req)


def _recency(last_play_ms, now_s):
    if not last_play_ms:
        return None
    days = max(0.0, (now_s - last_play_ms / 1000.0) / 86400.0)
    return math.exp(-days / RECENCY_HALFLIFE)


def _normalizer(values):
    """Min-max w obrebie puli. Zbyt maly rozrzut -> wszystko neutralnie."""
    vals = [v for v in values if v is not None]
    if not vals:
        return lambda x: NEUTRAL
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return lambda x: NEUTRAL
    return lambda x: NEUTRAL if x is None else (x - lo) / (hi - lo)


def score_rows(rows, stats, prior, weights, now_s, goal):
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    # --- surowe podwyniki ---
    for r in rows:
        st = stats.get(r["champion_id"]) or {}
        games = st.get("games") or 0
        req = r.get("next_grade")

        r["steps_remaining"] = max(0, goal - r["milestone"])
        r["games_played"] = games
        r["winrate_raw"] = (st["wins"] / games) if games else None
        r["best_grade"] = max(
            (g for g in (r.get("grades_earned") or []) if g in GRADE_RANK),
            key=lambda g: GRADE_RANK[g], default=None)

        r["_raw"] = {
            "grade_req": (1.0 - GRADE_RANK.get(req, MAX_RANK) / MAX_RANK) if req else None,
            "grade_hist": _grade_hist(r.get("grades_earned"), req),
            "winrate": _shrunk_winrate(st.get("wins") or 0, games, prior),
            "recency": _recency(r.get("last_play"), now_s),
            "ppg": (r["points"] / games) if games else None,
            "experience": math.log1p(r["points"]),
        }

    # --- normalizacja per podwynik, w obrebie puli ---
    norms = {
        k: _normalizer([r["_raw"][k] for r in rows])
        for k in DEFAULT_WEIGHTS
    }

    for r in rows:
        raw = r.pop("_raw")
        parts = {k: norms[k](raw[k]) for k in DEFAULT_WEIGHTS}
        num = sum(w[k] * v for k, v in parts.items())
        den = sum(w[k] for k in parts)
        r["score"] = round(100.0 * num / den, 1) if den else 0.0
        r["score_parts"] = {k: round(v, 3) for k, v in parts.items()}
        r["score_raw"] = {k: (round(v, 3) if v is not None else None) for k, v in raw.items()}

    # --- porzadek: najpierw najmniej szczebli, potem najwyzszy wynik ---
    rows.sort(key=lambda x: (x["steps_remaining"], -x["score"]))
    return rows
