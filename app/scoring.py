"""Dwupoziomowy ranking championow.

Poziom 1 (twardy): ile szczebli milestone zostalo do celu.
Poziom 2 (wynik 0..100): jak prawdopodobne, ze dowieziesz NASTEPNY szczebel.

Brakujace dane sa POMIJANE, nie zastepowane wartoscia neutralna - inaczej
wynik jest sredniá z polowek i wagi przestaja cokolwiek zmieniac. Zamiast
tego kazdy wiersz dostaje `confidence`: jaka czesc lacznej wagi opiera sie
na realnych danych.
"""

import math

from .db import GRADE_RANK

MAX_RANK = GRADE_RANK["S+"]

DEFAULT_WEIGHTS = {
    "grade_hist": 2.5,   # jak blisko progu byly Twoje oceny w tym milestonie
    "winrate":    1.0,   # WR w trybie, sciagniety do sredniej
    "recency":    1.2,   # swiezosc
    "ppg":        1.0,   # punkty maestrii na gre
    "experience": 1.0,   # obycie (log punktow)
}

SHRINK_K = 5.0
RECENCY_HALFLIFE = 30.0

# ile realnej wagi musi byc obecne, zeby wynik traktowac powaznie
CONFIDENCE_OK = 0.6


def _shrunk_winrate(wins, games, prior):
    if not games:
        return None
    return (wins + SHRINK_K * prior) / (games + SHRINK_K)


def _grade_hist(grades, required):
    """Najlepsza zebrana ocena wzgledem wymaganej.
    Brak ocen w tym milestonie = brak informacji, nie zero."""
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
    """Min-max w obrebie puli. None zostaje None."""
    vals = [v for v in values if v is not None]
    if not vals:
        return lambda x: None
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return lambda x: None if x is None else 0.5
    return lambda x: None if x is None else (x - lo) / (hi - lo)


def score_rows(rows, stats, prior, weights, now_s, goal):
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    w = {k: v for k, v in w.items() if k in DEFAULT_WEIGHTS}
    total_w = sum(w.values()) or 1.0

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
            "grade_hist": _grade_hist(r.get("grades_earned"), req),
            "winrate": _shrunk_winrate(st.get("wins") or 0, games, prior),
            "recency": _recency(r.get("last_play"), now_s),
            "ppg": (r["points"] / games) if games else None,
            "experience": math.log1p(r["points"]),
        }

    norms = {k: _normalizer([r["_raw"][k] for r in rows]) for k in DEFAULT_WEIGHTS}

    for r in rows:
        raw = r.pop("_raw")
        parts = {k: norms[k](raw[k]) for k in DEFAULT_WEIGHTS}
        present = {k: v for k, v in parts.items() if v is not None}

        den = sum(w[k] for k in present) or 1.0
        num = sum(w[k] * v for k, v in present.items())

        r["score"] = round(100.0 * num / den, 1) if present else 0.0
        r["confidence"] = round(sum(w[k] for k in present) / total_w, 2)
        r["thin"] = r["confidence"] < CONFIDENCE_OK
        r["score_parts"] = {k: (round(v, 3) if v is not None else None) for k, v in parts.items()}
        r["missing"] = [k for k, v in parts.items() if v is None]

    rows.sort(key=lambda x: (x["steps_remaining"], -x["score"]))
    return rows
