#!/usr/bin/env python3
"""
Powtorka hipotezy zmeczenia (bramka 14: >= 40 dokladnych ocen; otwarta 4.09
po odzysku ocen ze snapshotow, 42/40).

PROTOKOL - PREREJESTROWANY 4.09 PRZED spojrzeniem w dane (widzialem tylko
licznosci: 43 dokladne oceny KIWI z wierszem meczu). Zmiana ponizszych regul
po odpaleniu na danych uniewaznia analize.
  1. Proba: oceny DOKLADNE (nie cenzurowane) gier trybu misji z wierszem
     meczu (game_creation). Wynik = ranga oceny (GRADE_RANK, 0..14).
  2. Sesja: gry posortowane po starcie; nowa sesja, gdy przerwa miedzy
     koncem poprzedniej gry a startem nastepnej > 120 min. Pozycja
     w sesji = 1..n. Sesje jednogrowe nie nosza informacji o pozycji
     - wypadaja z obu testow (raportowane).
  3. Hipotezy, jednostronne, ustalone z gory:
     H_rozgrzewka: pierwsza gra sesji ma NIZSZA range niz pozostale gry
       tej sesji (statystyka: srednia rang gier na pozycji 1 minus srednia
       rang pozostalych; oczekiwana ujemna).
     H_zmeczenie: ranga MALEJE z pozycja w sesji (Spearman rho po wszystkich
       grach z sesji >= 2; prog |rho| >= 0.30 zgodnie z bramka).
  4. Istotnosc: test permutacyjny z tasowaniem rang WEWNATRZ sesji (2000
     permutacji, seed 7), alfa = 0.05/2 (Bonferroni po dwoch hipotezach).
  5. Konfundacja znajomosci championa (pierwsza gra dnia to czesto nowa
     postac): kontrola prerejestrowana - powtorka obu testow na REZYDUACH
     rangi po regresji liniowej na log(1 + liczba wczesniejszych gier misji
     tym championem). Werdykt SYGNAL wymaga zgodnosci testu surowego
     i rezydualnego (obie wersje przechodza prog i alfa).
  6. Moc: przy n~43 i rho = 0.30 moc Spearmana ~0.5 - "brak sygnalu" NIE
     jest dowodem braku efektu. Werdykty: SYGNAL / brak sygnalu (bramka
     wraca do czekania: powtorka przy 80 dokladnych ocenach) /
     nierozstrzygniety (test surowy i rezydualny niezgodne). Bez dogrywek,
     bez zmiany progow po fakcie.
  7. Wynik nie wchodzi do modelu ani rankingu - co najwyzej adnotacja w UI
     po decyzji czlowieka.

Uruchom na KOPII: DB_PATH=/sciezka/kopii.db python tools/fatigue_analysis.py
(kopia z JSON-a eksportu: tools/db_from_export.py)
"""
import math
import os
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GATE = 40
SESSION_GAP_S = 120 * 60
PERMS = 2000
SEED = 7
RHO_MIN = 0.30
ALPHA = 0.05 / 2


def _rank(values):
    """Rangi srednie (remisy) - do Spearmana."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def spearman(xs, ys):
    return _pearson(_rank(xs), _rank(ys))


def _residualize(ys, cond):
    n = len(ys)
    my, mc = sum(ys) / n, sum(cond) / n
    var = sum((c - mc) ** 2 for c in cond)
    beta = (sum((y - my) * (c - mc) for y, c in zip(ys, cond, strict=True)) / var) if var > 0 else 0.0
    return [y - (my + beta * (c - mc)) for y, c in zip(ys, cond, strict=True)]


def load_games(con):
    from app.db import GRADE_RANK, MODE_QUEUES
    modes = ", ".join(f"'{m}'" for m in MODE_QUEUES)
    rows = [dict(r) for r in con.execute(f"""
        SELECT g.match_id, g.grade, g.champion_id, m.game_creation / 1000 AS start,
               COALESCE(m.duration, 0) AS duration
        FROM grade_observation g JOIN match_player m ON m.match_id = g.match_id
        WHERE COALESCE(g.censored, 0) = 0 AND g.grade NOT LIKE '>=%'
          AND m.game_mode IN ({modes}) AND m.game_creation IS NOT NULL
        ORDER BY m.game_creation""")]
    allgames = [dict(r) for r in con.execute(f"""
        SELECT champion_id, game_creation / 1000 AS start FROM match_player
        WHERE game_mode IN ({modes}) AND game_creation IS NOT NULL""")]
    out = []
    for r in rows:
        rank = GRADE_RANK.get(r["grade"])
        if rank is None:
            continue
        prior = sum(1 for g in allgames
                    if g["champion_id"] == r["champion_id"] and g["start"] < r["start"])
        out.append({**r, "rank": rank, "prior": prior})
    return out


def sessions_of(games):
    """Dokleja session_id i pozycje; gry juz posortowane po starcie."""
    sid, pos, last_end = 0, 0, None
    for g in games:
        if last_end is None or g["start"] - last_end > SESSION_GAP_S:
            sid += 1
            pos = 0
        pos += 1
        g["session"], g["pos"] = sid, pos
        last_end = g["start"] + g["duration"]
    return games


def _tests(games, key):
    """Statystyki H_rozgrzewka i H_zmeczenie dla wartosci games[i][key]."""
    multi = [g for g in games if g["_n"] >= 2]
    first = [g[key] for g in multi if g["pos"] == 1]
    rest = [g[key] for g in multi if g["pos"] > 1]
    warm = (sum(first) / len(first) - sum(rest) / len(rest)) if first and rest else None
    rho = spearman([g["pos"] for g in multi], [g[key] for g in multi]) if len(multi) >= 3 else None
    return warm, rho


def _perm_p(games, key, obs_warm, obs_rho, rnd):
    """Tasowanie wartosci WEWNATRZ sesji zachowuje strukture sesji."""
    hits_w = hits_r = 0
    by_s = {}
    for g in games:
        by_s.setdefault(g["session"], []).append(g)
    for _ in range(PERMS):
        for lst in by_s.values():
            vals = [g[key] for g in lst]
            rnd.shuffle(vals)
            for g, v in zip(lst, vals, strict=True):
                g["_p"] = v
        w, r = _tests(games, "_p")
        if obs_warm is not None and w is not None and w <= obs_warm:
            hits_w += 1
        if obs_rho is not None and r is not None and r <= obs_rho:
            hits_r += 1
    return (hits_w + 1) / (PERMS + 1), (hits_r + 1) / (PERMS + 1)


def analyse(games):
    games = sessions_of(games)
    n_by = {}
    for g in games:
        n_by[g["session"]] = n_by.get(g["session"], 0) + 1
    for g in games:
        g["_n"] = n_by[g["session"]]
    resid = _residualize([g["rank"] for g in games],
                         [math.log1p(g["prior"]) for g in games])
    for g, r in zip(games, resid, strict=True):
        g["resid"] = r
    out = {}
    for key in ("rank", "resid"):
        warm, rho = _tests(games, key)
        pw, pr = _perm_p(games, key, warm, rho, random.Random(SEED))
        out[key] = {"warm_diff": warm, "warm_p": pw, "rho": rho, "rho_p": pr}
    multi = sum(1 for g in games if g["_n"] >= 2)
    out["n"] = len(games)
    out["sessions"] = len(n_by)
    out["multi_game"] = multi
    return out


def verdict(res):
    def passes(r, which):
        if which == "warm":
            return r["warm_diff"] is not None and r["warm_diff"] < 0 and r["warm_p"] < ALPHA
        return r["rho"] is not None and r["rho"] <= -RHO_MIN and r["rho_p"] < ALPHA
    out = {}
    for which in ("warm", "rho"):
        raw, res_ = passes(res["rank"], which), passes(res["resid"], which)
        out[which] = ("SYGNAL" if raw and res_ else
                      "nierozstrzygniety" if raw != res_ else "brak sygnalu")
    return out


def main(db_path=None, force=False):
    db_path = db_path or os.environ.get("DB_PATH")
    if not db_path:
        print("podaj kopie: DB_PATH=/sciezka/kopii.db")
        return 1
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    games = load_games(con)
    if len(games) < GATE and not force:
        print(f"bramka: {len(games)}/{GATE} dokladnych ocen - protokol zabrania "
              "patrzec na wynik przed progiem (--force = sam smoke)")
        return 2
    res = analyse(games)
    v = verdict(res)
    print(f"gier: {res['n']}, sesji: {res['sessions']}, gier w sesjach >= 2: "
          f"{res['multi_game']} (przerwa sesji > {SESSION_GAP_S // 60} min)")
    print(f"\n{'wersja':<10} {'rozgrz. diff':>13} {'p':>8} {'rho poz.':>9} {'p':>8}")
    for key, label in (("rank", "surowa"), ("resid", "rezyd.")):
        r = res[key]
        f = lambda x, d: "-" if x is None else f"{x:+.{d}f}"  # noqa: E731
        print(f"{label:<10} {f(r['warm_diff'], 2):>13} {r['warm_p']:>8.4f} "
              f"{f(r['rho'], 3):>9} {r['rho_p']:>8.4f}")
    print(f"\nWERDYKT rozgrzewka: {v['warm']}   zmeczenie: {v['rho']}")
    print("(SYGNAL = obie wersje przechodza prog i alfa; brak sygnalu = bramka "
          "wraca do czekania na 80 dokladnych; moc ~0.5 przy rho 0.30)")
    if force and len(games) < GATE:
        print("(smoke - bramka zamknieta, werdykt nie obowiazuje)")
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
