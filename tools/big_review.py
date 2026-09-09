#!/usr/bin/env python3
"""
Rewizja duza (bramka ">=100 obs", otwarta 9.09: 100/100): cztery karty ze
STAN w jednym gotowcu. PROTOKOL PREREJESTROWANY 9.09 PRZED spojrzeniem
w wynik (widzialem tylko liczniki: 100 obs treningowych, 58 par predykcji,
42 z next_p). Zmiana regul po odpaleniu uniewaznia test.

  1. (46) RZADKOSC: wariant D = produkcyjna baza (5 cech) + log1p(liczba
     wlasnych gier tym championem PRZED ta gra w trybie misji), z-score po
     probce. Harness identyczny jak w class_features_test (extract_features,
     _choose_l2, _loo_predictions, _threshold_metrics). Regula: WCHODZI
     tylko, gdy A- log-loss LOO spada >= 5 % I AUC(A-) nie spada > 0.02
     I S- log-loss nie rosnie > 5 %. Inaczej: odrzucony, (46) zostaje
     adnotacja w rankingu.
  2. kNN: alternatywny predyktor na tych samych 5 cechach (z-score po
     probce), LOO, k = 7, p = (trafienia + 2 * base_rate) / (k + 2).
     Porownanie z LOO regresji porzadkowej (baza). Regula: kNN "lepszy"
     tylko przy A- log-loss nizszym o >= 10 % I S- log-loss nie gorszym;
     inaczej zostaje regresja (zmiana strukturalna wymaga wyraznego zysku).
  3. CUSUM: pary predykcji (next_p, trafienie) progow A- i S- w kolejnosci
     czasu, S_t = suma (hit - p). Alarm dryfu, gdy max|S_t| > 2 * sqrt(suma
     p(1-p)) (~2 sigma) przy >= 10 parach. Inaczej: stabilny.
  4. (44b) KALIBRACJA PUL: (a) wiarygodnosc next_p progu A- w koszykach p
     (0-.2, .2-.4, .4-.6, .6-.8, .8-1): n, srednie p, trafienia;
     (b) pozycja wybranego championa wg next_p wsrod championow puli z tym
     samym progiem (1 / 2-3 / 4+) vs trafienie progu A-. Opisowo, bez reguly
     - "ranking dziala", gdy pozycja 1 ma trafienia >= reszty (przy malych n
     to obserwacja, nie dowod).
  5. Bez dogrywek: jedna siatka l2, jedno k, jeden prog alarmu.

Uruchom na KOPII: DB_PATH=/sciezka/kopii.db python tools/big_review.py
(kopia z JSON-a eksportu: tools/db_from_export.py)
"""
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GATE = 100
K = 7
PRIOR_W = 2.0
LL_GAIN_MIN = 0.05
AUC_DROP_MAX = 0.02
LL_LOSS_MAX_S = 0.05
KNN_GAIN_MIN = 0.10
BINS = [0.2, 0.4, 0.6, 0.8, 1.01]
CUSUM_MIN_N = 10


def rows_with_rarity(db, mode):
    with db.connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT g.grade, g.champion_id, g.match_id, m.kills, m.deaths,
                   m.assists, m.dmg_champ, m.gold, m.cs, m.vision, m.heal,
                   m.duration, m.dmg_taken, m.game_creation,
                   (SELECT COUNT(*) FROM match_player p
                     WHERE p.champion_id = m.champion_id
                       AND p.game_mode = m.game_mode
                       AND p.game_creation < m.game_creation) AS own_before
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            WHERE m.duration > 300 AND m.game_mode = ?
            ORDER BY m.game_creation""", (mode,))]


def zscore(values):
    n = len(values)
    m = sum(values) / n
    sd = (sum((v - m) ** 2 for v in values) / n) ** 0.5 or 1.0
    return [(v - m) / sd for v in values]


def build(model, rows, mode, rarity=False):
    baselines, gmed = model.champion_baselines(mode)
    X, specs, kept = [], [], []
    for r in rows:
        spec = model._parse_grade(r["grade"])
        if spec is None:
            continue
        f = model.extract_features(r, baselines, gmed, None, mode)
        X.append([f[k] for k in model.FEATURES])
        specs.append(spec)
        kept.append(r)
    if rarity:
        z = zscore([math.log1p(r["own_before"] or 0) for r in kept])
        X = [x + [v] for x, v in zip(X, z, strict=True)]
    return X, specs, kept


def evaluate(model, X, specs):
    l2, _report = model._choose_l2(X, specs)
    preds = model._loo_predictions(X, specs, l2, model.EPOCHS_VAL)
    return l2, {th: model._threshold_metrics(preds[th]) for th in model.THRESHOLDS}


def knn_loo(model, X, rows):
    """LOO kNN na z-score'owanych cechach; etykiety per prog z label_for."""
    cols = [zscore(list(c)) for c in zip(*X, strict=True)]
    Z = [list(t) for t in zip(*cols, strict=True)]
    out = {}
    for th in model.THRESHOLDS:
        labels = [model.label_for(r["grade"], th) for r in rows]
        idx = [i for i, y in enumerate(labels) if y is not None]
        if len(idx) < K + 2:
            out[th] = None
            continue
        base = sum(labels[i] for i in idx) / len(idx)
        preds = []
        for i in idx:
            near = sorted(
                (sum((a - b) ** 2 for a, b in zip(Z[i], Z[j], strict=True)), j)
                for j in idx if j != i)[:K]
            hits = sum(labels[j] for _, j in near)
            preds.append(((hits + PRIOR_W * base) / (K + PRIOR_W), labels[i]))
        out[th] = model._threshold_metrics(preds)
    return out


def decide_feature(base, var):
    a0, a1, s0, s1 = base.get("A-"), var.get("A-"), base.get("S-"), var.get("S-")
    if not (a0 and a1 and s0 and s1):
        return "nierozstrzygniety (brak metryk ktoregos progu)"
    gain = (a0["log_loss"] - a1["log_loss"]) / a0["log_loss"]
    auc_ok = (a1["auc"] or 0.0) >= (a0["auc"] or 0.0) - AUC_DROP_MAX
    s_ok = s1["log_loss"] <= s0["log_loss"] * (1 + LL_LOSS_MAX_S)
    ok = gain >= LL_GAIN_MIN and auc_ok and s_ok
    return (f"{'WCHODZI' if ok else 'odrzucony'} (A- log-loss {gain:+.1%}, "
            f"AUC A- {'ok' if auc_ok else 'spada > 0.02'}, "
            f"S- log-loss {'ok' if s_ok else 'rosnie > 5%'})")


def decide_knn(base, knn):
    a0, a1, s0, s1 = base.get("A-"), knn.get("A-"), base.get("S-"), knn.get("S-")
    if not (a0 and a1 and s0 and s1):
        return "nierozstrzygniety (brak metryk ktoregos progu)"
    gain = (a0["log_loss"] - a1["log_loss"]) / a0["log_loss"]
    s_ok = s1["log_loss"] <= s0["log_loss"]
    ok = gain >= KNN_GAIN_MIN and s_ok
    return (f"{'kNN LEPSZY' if ok else 'zostaje regresja'} (A- log-loss "
            f"{gain:+.1%} vs baza, S- {'nie gorszy' if s_ok else 'gorszy'})")


def cusum(pairs):
    """pairs: [(p, hit)] w kolejnosci czasu -> (max|S_t|, prog, alarm)."""
    s = smax = 0.0
    for p, h in pairs:
        s += h - p
        smax = max(smax, abs(s))
    var = sum(p * (1 - p) for p, _ in pairs)
    lim = 2 * math.sqrt(var) if var > 0 else 0.0
    return smax, lim, (len(pairs) >= CUSUM_MIN_N and smax > lim)


def rate_pairs(db, model):
    """(next_p, trafienie, prog, ts) rosnaco po czasie - tylko pary z next_p."""
    resolved, _pending = db.prediction_pairs()
    out = []
    for r in resolved:
        if r.get("next_p") is None:
            continue
        y = model.label_for(r["grade"], r["threshold"])
        if y is None:
            continue
        out.append((r["next_p"], y, r["threshold"], r["ts"]))
    return sorted(out, key=lambda t: t[3])


def pick_rank(db, model):
    """Pozycja wybranego championa wg next_p wsrod championow puli z tym
    samym progiem -> {'1': [hit...], '2-3': [...], '4+': [...]} dla progu A-."""
    with db.connect() as con:
        rows = con.execute("""
            SELECT g.grade,
                   (SELECT COUNT(*) + 1 FROM pool_prediction q
                     WHERE q.pool_id = csp.id AND q.threshold = pp.threshold
                       AND q.next_p > pp.next_p) AS rank
            FROM champ_select_pool csp
            JOIN pool_prediction pp
              ON pp.pool_id = csp.id AND pp.champion_id = csp.picked_id
            JOIN grade_observation g ON g.match_id = csp.match_id
            WHERE pp.next_p IS NOT NULL AND pp.threshold = 'A-'""").fetchall()
    out = {"1": [], "2-3": [], "4+": []}
    for r in rows:
        y = model.label_for(r["grade"], "A-")
        if y is None:
            continue
        key = "1" if r["rank"] == 1 else ("2-3" if r["rank"] <= 3 else "4+")
        out[key].append(y)
    return out


def _fmt(d, k):
    return "-" if not d or d.get(k) is None else f"{d[k]:.3f}"


def main(db_path=None):
    db_path = db_path or os.environ.get("DB_PATH")
    if not db_path:
        print("podaj kopie: DB_PATH=/sciezka/kopii.db")
        return 1
    from app import db, model
    db.DB_PATH = Path(db_path)
    mode = os.environ.get("DEFAULT_MODE", "KIWI")
    rows = rows_with_rarity(db, mode)
    if len(rows) < GATE:
        print(f"bramka: {len(rows)}/{GATE} obserwacji - protokol zabrania "
              "patrzec na wynik przed progiem")
        return 2
    print(f"obserwacji treningowych: {len(rows)} (tryb {mode})\n")

    # 1 + 2: rzadkosc i kNN na tym samym harnessie
    X0, specs0, kept = build(model, rows, mode, rarity=False)
    X1, specs1, _ = build(model, rows, mode, rarity=True)
    l2a, base = evaluate(model, X0, specs0)
    l2d, rar = evaluate(model, X1, specs1)
    knn = knn_loo(model, X0, kept)
    print(f"{'wariant':<22} {'l2':>4} {'A- ll':>7} {'A- auc':>7} {'S- ll':>7} {'S- auc':>7}")
    for label, l2, met in (("baza (5 cech)", l2a, base),
                           ("D + rzadkosc (46)", l2d, rar),
                           ("kNN k=7", "-", knn)):
        a, s = met.get("A-") or {}, met.get("S-") or {}
        print(f"{label:<22} {str(l2):>4} {_fmt(a, 'log_loss'):>7} {_fmt(a, 'auc'):>7} "
              f"{_fmt(s, 'log_loss'):>7} {_fmt(s, 'auc'):>7}")
    print(f"\nWERDYKT (46) rzadkosc: {decide_feature(base, rar)}")
    print(f"WERDYKT kNN: {decide_knn(base, knn)}")

    # 3: CUSUM
    pairs = rate_pairs(db, model)
    print(f"\nCUSUM (pary z next_p w kolejnosci czasu, razem {len(pairs)}):")
    for th in model.THRESHOLDS:
        sub = [(p, y) for p, y, t, _ in pairs if t == th]
        smax, lim, alarm = cusum(sub)
        print(f"  {th}: n={len(sub)} max|S|={smax:.2f} prog={lim:.2f} -> "
              f"{'DRYF' if alarm else 'stabilny'}")

    # 4: kalibracja pul
    print("\n(44b) wiarygodnosc next_p progu A- w koszykach p:")
    sub = [(p, y) for p, y, t, _ in pairs if t == "A-"]
    lo = 0.0
    for edge in BINS:
        b = [(p, y) for p, y in sub if lo <= p < edge]
        if b:
            print(f"  p {lo:.1f}-{min(edge, 1.0):.1f}: n={len(b)} srednie p="
                  f"{sum(p for p, _ in b) / len(b):.2f} trafienia="
                  f"{sum(y for _, y in b) / len(b):.2f}")
        lo = edge
    ranks = pick_rank(db, model)
    print("(44b) pozycja wybranego championa wg next_p (prog A-) vs trafienie:")
    rates = {}
    for key, ys in ranks.items():
        rates[key] = (sum(ys) / len(ys)) if ys else None
        print(f"  pozycja {key:<4} n={len(ys):3d} trafienia="
              f"{'-' if rates[key] is None else f'{rates[key]:.2f}'}")
    r1, rest = rates.get("1"), [v for k, v in rates.items() if k != "1" and v is not None]
    if r1 is None or not rest:
        print("WERDYKT (44b): za malo danych o pozycji wyboru")
    else:
        print(f"WERDYKT (44b): {'ranking dziala' if r1 >= max(rest) else 'ranking NIE wyprzedza reszty'}"
              f" (pozycja 1: {r1:.2f} vs reszta max {max(rest):.2f}; obserwacja, nie dowod)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
