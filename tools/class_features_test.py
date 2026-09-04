#!/usr/bin/env python3
"""
Test przyrostu informacji z cech klasowych (bramka "cechy klasowe ~60 obs",
otwarta 4.09 po odzysku ocen: 62/60).

PROTOKOL - PREREJESTROWANY 4.09 PRZED spojrzeniem w wynik (widzialem tylko
licznosci: 63 oceny, 43 dokladne KIWI, 340 k wierszy player_stat). Zmiana
regul po odpaleniu uniewaznia test.
  1. Baza A: DOKLADNIE produkcyjna sciezka modelu - wiersze treningowe
     trybu misji, extract_features (5 cech), _parse_grade, _choose_l2
     (siatka L2_GRID, EPOCHS_TUNE), _loo_predictions z wybranym l2
     i EPOCHS_VAL, _threshold_metrics per prog. Zadnej wlasnej imitacji.
  2. Wariant B = A + taken_z: obrazenia OTRZYMANE na minute jako z-score
     wzgledem norm tego championa (db.norm_z, klucz totalDamageTaken, tryb
     misji; brak norm = 0.0). Wariant C = B + mitigated_z: obrazenia
     ZLAGODZONE na minute (damageSelfMitigated z wlasnego wiersza
     player_stat, z-score jak wyzej) - liczony TYLKO, gdy pokrycie >= 90 %
     wierszy treningowych; inaczej raportowany jako pominiety.
  3. Kazdy wariant wybiera swoje l2 ta sama procedura co baza.
  4. Regula decyzji: wariant WCHODZI do modelu tylko, gdy na progu A-
     log-loss LOO spada o >= 5 % wzgledem bazy I AUC(A-) nie spada o wiecej
     niz 0.02, ORAZ na progu S- log-loss nie rosnie o wiecej niz 5 %.
     Kazdy inny wynik = odrzucony, bramka wraca do czekania na 100
     obserwacji. Ruch AUC < 0.1 to szum (Hanley-McNeil) - dlatego
     kryterium jest log-loss, a AUC tylko "nie gorzej".
  5. Bez dogrywek: jedna siatka l2, brak zmiany progow; regresja
     porzadkowa startuje z zer, wiec przebieg jest deterministyczny.

Uruchom na KOPII: DB_PATH=/sciezka/kopii.db python tools/class_features_test.py
(kopia z JSON-a eksportu: tools/db_from_export.py)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GATE = 60
COVERAGE_MIN = 0.90
LL_GAIN_MIN = 0.05
AUC_DROP_MAX = 0.02
LL_LOSS_MAX_S = 0.05
VARIANTS = {"A": "baza (5 cech)", "B": "A + taken_z", "C": "B + mitigated_z"}


def rows_with_extras(db, mode):
    with db.connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT g.grade, g.champion_id, g.match_id, m.kills, m.deaths,
                   m.assists, m.dmg_champ, m.gold, m.cs, m.vision, m.heal,
                   m.duration, m.dmg_taken,
                   (SELECT p.stat_value FROM player_stat p
                     WHERE p.match_id = m.match_id AND p.is_local = 1
                       AND p.stat_key = 'damageSelfMitigated' LIMIT 1) AS mitigated
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            WHERE m.duration > 300 AND m.game_mode = ?""", (mode,))]


def build(db, model, feat, rows, mode, variant):
    baselines, gmed = model.champion_baselines(mode)
    X, specs, covered, cache = [], [], 0, {}
    for r in rows:
        spec = model._parse_grade(r["grade"])
        if spec is None:
            continue
        f = model.extract_features(r, baselines, gmed, None, mode)
        x = [f[k] for k in model.FEATURES]
        mins = feat.minutes(r["duration"])
        if variant in ("B", "C"):
            z = db.norm_z(r["champion_id"], "totalDamageTaken",
                          (r["dmg_taken"] or 0) / mins, mode, cache)
            x.append(z["z"] if z else 0.0)
        if variant == "C":
            if r["mitigated"] is not None:
                covered += 1
                z = db.norm_z(r["champion_id"], "damageSelfMitigated",
                              (r["mitigated"] or 0) / mins, mode, cache)
                x.append(z["z"] if z else 0.0)
            else:
                x.append(0.0)
        X.append(x)
        specs.append(spec)
    return X, specs, (covered / len(X) if X else 0.0)


def evaluate(model, X, specs):
    l2, _report = model._choose_l2(X, specs)
    preds = model._loo_predictions(X, specs, l2, model.EPOCHS_VAL)
    return l2, {th: model._threshold_metrics(preds[th]) for th in model.THRESHOLDS}


def decide(base, var):
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


def main(db_path=None):
    db_path = db_path or os.environ.get("DB_PATH")
    if not db_path:
        print("podaj kopie: DB_PATH=/sciezka/kopii.db")
        return 1
    from app import db, features, model
    db.DB_PATH = Path(db_path)
    mode = os.environ.get("DEFAULT_MODE", "KIWI")
    rows = rows_with_extras(db, mode)
    if len(rows) < GATE:
        print(f"bramka: {len(rows)}/{GATE} obserwacji - protokol zabrania patrzec "
              "na wynik przed progiem")
        return 2
    results = {}
    for v in VARIANTS:
        X, specs, cover = build(db, model, features, rows, mode, v)
        if v == "C" and cover < COVERAGE_MIN:
            results[v] = {"skipped": f"pokrycie mitigated {cover:.0%} < {COVERAGE_MIN:.0%}"}
            continue
        l2, met = evaluate(model, X, specs)
        results[v] = {"l2": l2, "cover": cover, "n": len(X), **met}

    print(f"obserwacji treningowych: {len(rows)} (tryb {mode})\n")
    print(f"{'wariant':<18} {'l2':>4} {'A- ll':>7} {'A- auc':>7} {'S- ll':>7} {'S- auc':>7}")
    for v, label in VARIANTS.items():
        r = results[v]
        if "skipped" in r:
            print(f"{label:<18} {r['skipped']}")
            continue
        a, s = r.get("A-") or {}, r.get("S-") or {}
        fmt = lambda d, k: "-" if not d or d.get(k) is None else f"{d[k]:.3f}"  # noqa: E731
        print(f"{label:<18} {r['l2']:>4} {fmt(a, 'log_loss'):>7} {fmt(a, 'auc'):>7} "
              f"{fmt(s, 'log_loss'):>7} {fmt(s, 'auc'):>7}")
    print()
    for v in ("B", "C"):
        if "skipped" in results[v]:
            print(f"WERDYKT {v}: pominiety - {results[v]['skipped']}")
        else:
            print(f"WERDYKT {v}: {decide(results['A'], results[v])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
