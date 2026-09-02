#!/usr/bin/env python3
"""
Gotowiec analizy timingu pod bramke "rewizja eventdata po 50 grach".

PROTOKOL - PREREJESTROWANY 2.09, PRZED zobaczeniem pelnych danych
(pisany przy 13 grach w logu; zmiana ponizszych regul po otwarciu bramki
uniewaznia analize):
  1. Prog wejscia: >= 50 gier w live_event_log (bramka STAN). Ponizej
     skrypt ODMAWIA analizy (rc=2); --force robi wylacznie smoke
     parsowania (profile bez werdyktu).
  2. Metryki timingu per gra: deaths_0_5, deaths_5_10, first_death_s,
     kills_early_share (udzial killi przed 10. minuta).
  3. Sukces = ocena >= A- (label_for), dopasowana po championie
     i |saved_at - observed_at| <= 900 s. Gra bez dopasowanej oceny
     wypada z proby (raportowana osobno).
  4. Test PRZYROSTU informacji ponad laczna liczbe smierci: kazda metryke
     rezydualizujemy liniowo po deaths_total, liczymy korelacje Pearsona
     rezyduow z sukcesem, istotnosc testem permutacyjnym (2000 permutacji
     etykiet, seed 7). Bez warunkowania wynik bylby tautologia - ocena
     juz karze za smierci (warunek sedziego E).
  5. Progi: |r| >= 0.30 ORAZ p < 0.05/4 (Bonferroni po 4 metrykach).
  6. Werdykt nierozstrzygniety = KASACJA zbierania eventdata, zgodnie
     z trescia bramki. Bez dogrywek i bez zmiany progow po fakcie.

Uruchom na KOPII bazy: DB_PATH=/sciezka/kopii.db python tools/timing_analysis.py
"""
import json
import os
import random
import sqlite3
import sys

GATE = 50
WINDOW_S = 900
PERMS = 2000
R_MIN = 0.30
ALPHA = 0.05 / 4

METRICS = ["deaths_0_5", "deaths_5_10", "first_death_s", "kills_early_share"]


def _is_me(name, me):
    return bool(name) and bool(me) and (name == me or str(name).startswith(me))


def game_profile(events, me):
    """Surowe eventy Live Client -> profil timingu jednej gry."""
    deaths, kills = [], []
    for e in events or []:
        if e.get("EventName") != "ChampionKill":
            continue
        t = float(e.get("EventTime") or 0)
        if _is_me(e.get("VictimName"), me):
            deaths.append(t)
        if _is_me(e.get("KillerName"), me):
            kills.append(t)
    early_k = sum(1 for t in kills if t < 600)
    return {
        "deaths_total": len(deaths),
        "deaths_0_5": sum(1 for t in deaths if t < 300),
        "deaths_5_10": sum(1 for t in deaths if 300 <= t < 600),
        "first_death_s": min(deaths) if deaths else None,
        "kills_early_share": (early_k / len(kills)) if kills else None,
    }


def _residualize(xs, cond):
    """Rezyduum x po liniowej regresji na cond (czysty Python)."""
    n = len(xs)
    mx, mc = sum(xs) / n, sum(cond) / n
    var = sum((c - mc) ** 2 for c in cond)
    beta = (sum((x - mx) * (c - mc) for x, c in zip(xs, cond, strict=True))
            / var) if var > 0 else 0.0
    return [x - (mx + beta * (c - mc)) for x, c in zip(xs, cond, strict=True)]


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def _perm_p(xs, ys, rnd):
    obs = abs(_pearson(xs, ys))
    hits = 0
    ys2 = list(ys)
    for _ in range(PERMS):
        rnd.shuffle(ys2)
        if abs(_pearson(xs, ys2)) >= obs:
            hits += 1
    return (hits + 1) / (PERMS + 1)


def load_samples(con, me):
    """Profil + sukces per gra; dopasowanie oceny po championie i czasie."""
    logs = con.execute(
        "SELECT saved_at, champion_id, events FROM live_event_log").fetchall()
    grades = con.execute(
        "SELECT champion_id, grade, observed_at FROM grade_observation"
    ).fetchall()
    from app.model import label_for
    out, unmatched = [], 0
    for row in logs:
        prof = game_profile(json.loads(row["events"]), me)
        best = None
        for g in grades:
            if g["champion_id"] != row["champion_id"]:
                continue
            gap = abs((g["observed_at"] or 0) - row["saved_at"])
            if gap <= WINDOW_S and (best is None or gap < best[0]):
                best = (gap, g["grade"])
        if best is None:
            unmatched += 1
            continue
        y = label_for(best[1], "A-")
        if y is None:
            unmatched += 1
            continue
        out.append((prof, y))
    return out, unmatched


def main(db_path=None, force=False, me=None):
    db_path = db_path or os.environ.get("DB_PATH")
    if not db_path:
        print("podaj kopie: DB_PATH=/sciezka/kopii.db")
        return 1
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    n = con.execute("SELECT COUNT(*) c FROM live_event_log").fetchone()["c"]
    if n < GATE and not force:
        print(f"bramka eventdata: {n}/{GATE} gier - protokol zabrania "
              "patrzec na wynik przed progiem (--force = sam smoke parsowania)")
        return 2

    me = me or os.environ.get("MY_RIOT_FULL") or ""
    samples, unmatched = load_samples(con, me)
    print(f"gier w logu: {n}, z dopasowana ocena: {len(samples)}, "
          f"bez dopasowania: {unmatched}")
    if force and n < GATE:
        for prof, y in samples[:10]:
            print(f"  y={y} {prof}")
        print("(smoke parsowania - bez werdyktu, bramka zamknieta)")
        return 0

    conds = [p["deaths_total"] for p, _ in samples]
    verdicts = []
    rnd = random.Random(7)
    for m in METRICS:
        pairs = [(p[m], y, c) for (p, y), c in zip(samples, conds, strict=True)
                 if p[m] is not None]
        if len(pairs) < GATE // 2:
            verdicts.append((m, None, None, "za malo danych"))
            continue
        xs = _residualize([x for x, _, _ in pairs], [c for _, _, c in pairs])
        yy = [y for _, y, _ in pairs]
        r = _pearson(xs, yy)
        p = _perm_p(xs, yy, rnd)
        ok = abs(r) >= R_MIN and p < ALPHA
        verdicts.append((m, r, p, "SYGNAL" if ok else "szum"))
    print(f"\n{'metryka':<20} {'r(rezyd.)':>10} {'p(perm)':>9}  werdykt")
    for m, r, p, v in verdicts:
        rs = f"{r:+.3f}" if r is not None else "-"
        ps = f"{p:.4f}" if p is not None else "-"
        print(f"{m:<20} {rs:>10} {ps:>9}  {v}")
    if not any(v == "SYGNAL" for _, _, _, v in verdicts):
        print("\nWERDYKT: brak sygnalu ponad liczbe smierci -> wg tresci "
              "bramki: KASACJA zbierania eventdata")
    else:
        print("\nWERDYKT: timing niesie informacje - decyzja o metrykach "
              "do STAN po przegladzie wynikow")
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
