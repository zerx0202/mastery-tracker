"""
Model prawdopodobienstwa oceny pomeczowej.

Regresja porzadkowa (proportional odds) z cenzurowaniem:
    P(ocena >= k | x) = sigmoid(beta * x - alpha_k),  alpha rosnace po drabince

Jeden wspolny wektor wag beta dla calej skali ocen + osobny prog alpha_k
dla kazdego szczebla. Obserwacja dokladna (np. "B+") wnosi do wiarygodnosci
P(>=B+) - P(>=nastepny); cenzurowana (">=A-" z awansu milestone'a) wnosi
wprost P(>=A-). Dzieki temu:
  - kazda z obserwacji karmi WSZYSTKIE progi naraz (model S- pozycza sile
    od pelnej probki zamiast stac na garstce pozytywow),
  - P(>=A-) >= P(>=S-) z konstrukcji - dwa niezalezne modele potrafily
    te nierownosc zlamac i nic tego nie pilnowalo.
Zalozenie proportional odds (ten sam kierunek wplywu cech na kazdym progu)
jest mocne, ale przy tej probce to lepszy prior niz estymacja S- z 3
pozytywow. Do rewizji przy ~100 obserwacjach.

Dlaczego obrazenia sa normalizowane przez championa: w danych B+ ma
NIZSZE obrazenia na minute niz C, bo B+ to postacie utility. Ocena jest
liczona wzgledem innych grajacych ta postacia, wiec surowa wartosc jest
mylaca. gold/min jest za to monotoniczne i nie wymaga normalizacji.

UWAGA (nazwany, niewyeliminowany wyciek): norm_z liczy sie z pelnego
player_stat, wiec w walidacji LOO fold widzi 1 wiersz statystyk wlasnej
odlozonej gry. Przy normach zdominowanych przez snowball (10 obs/mecz,
setki gier) to wplyw rzedu 1/n na jedna ceche - pomijalny, ale jesli
metryki kiedys wygladaja podejrzanie dobrze, zaczac szukac tutaj.
"""

import json
import math
import statistics
import time

from . import features
from .db import (
    GRADE_RANK,
    GRADES,
    connect,
    get_json_setting,
    log_event,
    norm_z,
    set_json_setting,
)

# Progi, dla ktorych trenujemy osobne modele
THRESHOLDS = ["A-", "S-"]

# Cechy niezalezne od postaci + jedna znormalizowana
FEATURES = [
    "gold_per_min",
    "ka_per_min",
    "deaths_per_min",
    "dmg_ratio",        # obrazenia / mediana na tym championie
    "duration_min",
]

MIN_SAMPLES = 20        # ponizej tego model jest tylko poglądowy


# ---------- cechy ----------

def champion_baselines(mode=None):
    """Mediana obrazen na minute per champion, z WSZYSTKICH meczow
    (nie tylko ocenionych) - wiecej danych na normalizator."""
    clause = "AND game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        rows = [dict(r) for r in con.execute(f"""
            SELECT champion_id, dmg_champ, duration FROM match_player
            WHERE duration > 300 AND dmg_champ IS NOT NULL {clause}""", args)]

    per_champ = {}
    all_dpm = []
    for r in rows:
        dpm = r["dmg_champ"] / (r["duration"] / 60)
        per_champ.setdefault(r["champion_id"], []).append(dpm)
        all_dpm.append(dpm)

    global_median = statistics.median(all_dpm) if all_dpm else 1.0
    return ({c: statistics.median(v) for c, v in per_champ.items()}, global_median)


# Mapowanie cech modelu na klucze w player_stat. Normalizujemy WYLACZNIE
# obrazenia (gold/min jest monotoniczne z ocena i normalizacji nie wymaga -
# martwy wpis goldEarned sugerowal co innego i zapraszal do "wpiecia").
NORM_KEYS = {
    "dmg_ratio": "totalDamageDealtToChampions",
}


def extract_features(row, baselines, global_median, external=None, mode=None):
    """external zostaje dla zgodnosci, ale zrodlem normalizacji jest teraz
    rozklad z player_stat - dane z Mayhema, nie ze zwyklego ARAM-a."""
    fv = features.match_features(row)
    mins = fv["minutes"]
    cid = row.get("champion_id")
    dpm = fv["dpm"]
    gpm = fv["gpm"]

    # obrazenia: z-score wzgledem tego championa, z fallbackiem na stara
    # metode (wlasna mediana), gdy player_stat jeszcze nic nie wie
    zd = norm_z(cid, NORM_KEYS["dmg_ratio"], dpm, mode) if cid else None
    if zd and zd["observations"] >= 1:
        dmg_feature = zd["z"]
    else:
        base = None
        if external:
            base = external.get(cid)
        if base is None:
            own = baselines.get(cid)
            base = own if own and own > 0 else global_median
        # stara miara byla ilorazem wokol 1.0; przesuwamy na te sama skale
        dmg_feature = (dpm / base - 1.0) if base else 0.0

    return {
        "gold_per_min": gpm,
        "ka_per_min": fv["ka_per_min"],
        "deaths_per_min": fv["deaths_per_min"],
        "dmg_ratio": dmg_feature,
        "duration_min": mins,
    }


# ---------- etykiety ----------

def label_for(grade, threshold):
    """Zwraca 1 / 0 / None. None = obserwacja nie niesie informacji o tym progu."""
    want = GRADE_RANK.get(threshold)
    if want is None:
        return None

    if grade.startswith(">="):
        got = GRADE_RANK.get(grade[2:].strip())
        if got is None:
            return None
        # ">=A-" mowi: bylo A- lub lepiej. Dla progu A- to sukces.
        # Dla progu S- nie wiemy nic - moglo byc A-, moglo S+.
        return 1 if got >= want else None

    got = GRADE_RANK.get(grade)
    if got is None:
        return None
    return 1 if got >= want else 0


def training_rows(mode=None):
    clause = "AND m.game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        return [dict(r) for r in con.execute(f"""
            SELECT g.grade, g.champion_id, m.kills, m.deaths, m.assists,
                   m.dmg_champ, m.gold, m.cs, m.vision, m.heal, m.duration
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            WHERE m.duration > 300 {clause}""", args)]


# ---------- regresja porzadkowa ----------

# Progi mission-critical + stale strojenia. TUNE/VAL maja mniej epok niz
# trening koncowy - to swiadomy kompromis czasu (train() chodzi po kazdej
# ocenie w tle na Airze), a wybor lambdy i ranking foldow sa na to odporne.
L2_GRID = [0.3, 1.0, 3.0]
EPOCHS_FINAL = 2500
EPOCHS_TUNE = 500
EPOCHS_VAL = 900
LR_ORD = 0.15


def _standardize(matrix):
    n_feat = len(matrix[0])
    means, stds = [], []
    for j in range(n_feat):
        col = [row[j] for row in matrix]
        mu = sum(col) / len(col)
        sd = statistics.pstdev(col) or 1.0
        means.append(mu)
        stds.append(sd)
    scaled = [[(row[j] - means[j]) / stds[j] for j in range(n_feat)] for row in matrix]
    return scaled, means, stds


def _sigmoid(z):
    if z < -30:
        return 1e-13
    if z > 30:
        return 1 - 1e-13
    return 1 / (1 + math.exp(-z))


def _parse_grade(grade):
    """("censored", rank) dla ">=X", ("exact", rank) dla "X", None gdy smiec."""
    if not isinstance(grade, str):
        return None
    if grade.startswith(">="):
        r = GRADE_RANK.get(grade[2:].strip())
        return ("censored", r) if r is not None else None
    r = GRADE_RANK.get(grade.strip())
    return ("exact", r) if r is not None else None


def _cutpoints(specs):
    """Progi alpha: kazdy rank, dla ktorego wiarygodnosc potrzebuje P(>=r):
    oceny dokladne (prog wlasny i nastepny w gore), progi cenzurowania,
    zawsze progi misji. Najnizszy rank odpada - P(>=min) = 1 z definicji."""
    ranks = {GRADE_RANK[t] for t in THRESHOLDS}
    for _kind, r in specs:
        ranks.add(r)
    ranks = sorted(ranks)
    return ranks[1:] if len(ranks) > 1 else ranks


def _alpha_vector(a0, gaps):
    """Progi rosnace z konstrukcji: alpha_k = a0 + suma exp(gap)."""
    out = [a0]
    for g in gaps:
        out.append(out[-1] + math.exp(g))
    return out


def _fit_ordinal(X, specs, cuts, l2, epochs, lr):
    """Proportional odds z cenzurowaniem, czysty Python, pelny gradient.
    L2 tylko na beta - progi alpha to polozenia na skali, nie wagi cech."""
    n, k = len(X), len(X[0])
    ncut = len(cuts)
    cut_ix = {r: i for i, r in enumerate(cuts)}
    # nastepny prog W GORE od danego ranku (do komorek ocen dokladnych)
    next_up = {}
    for r in {r for _, r in specs}:
        above = [c for c in cuts if c > r]
        next_up[r] = above[0] if above else None

    # start: progi z czestosci brzegowych (etykiety per prog), beta = 0
    beta = [0.0] * k
    a0, gaps = 0.0, [0.0] * (ncut - 1)
    # start: alpha z czestosci brzegowych. "Na pewno >= c" to exact r>=c
    # albo censored r>=c; exact r<c to pewne 0; censored r<c - niewiadoma.
    marg = []
    for c in cuts:
        pos = sum(1 for kind, r in specs if r >= c)
        neg = sum(1 for kind, r in specs if kind == "exact" and r < c)
        tot = pos + neg
        p = min(max(pos / tot if tot else 0.5, 0.02), 0.98)
        marg.append(-math.log(p / (1 - p)))
    # wymuszenie rosnacosci na starcie
    for i in range(1, ncut):
        marg[i] = max(marg[i], marg[i - 1] + 1e-3)
    a0 = marg[0]
    gaps = [math.log(max(marg[i] - marg[i - 1], 1e-3)) for i in range(1, ncut)]

    for _ in range(epochs):
        alphas = _alpha_vector(a0, gaps)
        gb = [0.0] * k
        ga0 = 0.0
        ggaps = [0.0] * (ncut - 1)

        for xi, (kind, r) in zip(X, specs, strict=False):
            u = sum(beta[j] * xi[j] for j in range(k))
            # gradient log-wiarygodnosci po u i po konkretnych alpha
            if kind == "censored":
                lo = cut_ix.get(r)
                if lo is None:
                    # rank rowny minimum wypada z cuts (P(>=min)=1), wiec
                    # log-wiarygodnosc to log 1 = 0 - obserwacja nie niesie
                    # gradientu. Twarde cut_ix[r] rzucalo tu KeyError
                    # i klalo CALY trening w foldzie LOO bez zadnej
                    # dokladnej oceny ponizej progu (wczesna probka).
                    continue
                s = _sigmoid(u - alphas[lo])
                du = 1 - s
                dalpha = {lo: -(1 - s)}
            else:
                lo = cut_ix.get(r)          # None = najnizszy rank (P(>=r)=1)
                hi_r = next_up[r]
                hi = cut_ix.get(hi_r) if hi_r is not None else None
                s_lo = _sigmoid(u - alphas[lo]) if lo is not None else 1.0
                s_hi = _sigmoid(u - alphas[hi]) if hi is not None else 0.0
                p = max(s_lo - s_hi, 1e-12)
                d_lo = s_lo * (1 - s_lo) if lo is not None else 0.0
                d_hi = s_hi * (1 - s_hi) if hi is not None else 0.0
                du = (d_lo - d_hi) / p
                dalpha = {}
                if lo is not None:
                    dalpha[lo] = -d_lo / p
                if hi is not None:
                    dalpha[hi] = dalpha.get(hi, 0.0) + d_hi / p
            for j in range(k):
                gb[j] += du * xi[j]
            for ix, dv in dalpha.items():
                ga0 += dv
                for gi in range(ix):        # alpha_ix zalezy od gaps[0..ix-1]
                    ggaps[gi] += dv * math.exp(gaps[gi])

        for j in range(k):
            beta[j] += lr * (gb[j] / n - l2 * beta[j] / n)
        a0 += lr * ga0 / n
        for gi in range(ncut - 1):
            gaps[gi] += lr * ggaps[gi] / n

    return beta, {c: a for c, a in zip(cuts, _alpha_vector(a0, gaps), strict=False)}


def _p_ge(x_std, beta, alphas, rank):
    u = sum(beta[j] * x_std[j] for j in range(len(beta)))
    return _sigmoid(u - alphas[rank])


def _auc_ci(auc, n_pos, n_neg):
    """Hanley-McNeil: SE i 95% CI. Zeby ruchy AUC mniejsze niz szum
    przestaly uchodzic za wynik. Interpretacja (przeglad 2.09): regula
    "ruchy <0.1 to szum" jest dobra dla progu A-; dla S- przy <10
    pozytywach SE >= 0.12-0.15, wiec tam szumem sa i ruchy ~0.15.
    SE jest przyblizeniem - predykcje LOO sa skorelowane miedzy foldami."""
    if auc is None or not n_pos or not n_neg:
        return None, None
    q1 = auc / (2 - auc)
    q2 = 2 * auc * auc / (1 + auc)
    var = (auc * (1 - auc) + (n_pos - 1) * (q1 - auc * auc)
           + (n_neg - 1) * (q2 - auc * auc)) / (n_pos * n_neg)
    se = math.sqrt(max(var, 0.0))
    return round(se, 3), [round(max(0.0, auc - 1.96 * se), 3),
                          round(min(1.0, auc + 1.96 * se), 3)]


def _threshold_metrics(preds):
    """Metryki dla listy (p, etykieta) jednego progu."""
    if not preds:
        return None
    y = [t for _, t in preds]
    if len(set(y)) < 2:
        return None
    correct = sum(1 for p, t in preds if (p >= 0.5) == bool(t))
    ll = sum(t * math.log(max(p, 1e-13)) + (1 - t) * math.log(max(1 - p, 1e-13))
             for p, t in preds)
    base = sum(y) / len(y)
    base_acc = max(base, 1 - base)
    # log-loss stalego predyktora p=base_rate - punkt odniesienia bramki
    # useful. accuracy@0.5 przy base rate S- ~0.1-0.2 jest praktycznie
    # nie do pobicia inaczej niz przerzuceniem pozytywow nad 0.5, wiec
    # stara bramka byla dla S- de facto nieosiagalna i mierzyla nie to,
    # co konsumuje E(c)=suma 1/p (przeglad 2.09, W3).
    base_ll = -(base * math.log(max(base, 1e-13))
                + (1 - base) * math.log(max(1 - base, 1e-13)))
    pos = [p for p, t in preds if t == 1]
    neg = [p for p, t in preds if t == 0]
    auc = None
    if pos and neg:
        wins = sum(1 for a in pos for b_ in neg if a > b_)
        ties = sum(1 for a in pos for b_ in neg if a == b_)
        auc = (wins + 0.5 * ties) / (len(pos) * len(neg))
    acc = correct / len(preds)
    se, ci = _auc_ci(auc, len(pos), len(neg))
    return {
        "method": "leave-one-out (porzadkowa)",
        "tested": len(preds),
        "accuracy": round(acc, 3),
        "baseline_accuracy": round(base_acc, 3),
        "lift": round(acc - base_acc, 3),
        "auc": round(auc, 3) if auc is not None else None,
        "auc_se": se,
        "auc_ci95": ci,
        "log_loss": round(-ll / len(preds), 4),
        "baseline_log_loss": round(base_ll, 4),
        "base_rate": round(base, 3),
        "useful": bool(auc is not None and auc >= 0.65
                       and (-ll / len(preds)) < base_ll),
    }


def calibration_stats(pairs):
    """(B1/W4) Kalibracja garstki predykcji sprzed gry: Brier, hit_rate
    z 95% CI Wilsona, srednie p i test Z Spiegelhaltera (czy Brier jest
    gorszy, niz wynika z samych p; |Z| > ~2 = kalibracja odrzucona).
    Liczone per prog i per zrodlo p, bo laczny Brier progow o roznych
    base rate jest nieinterpretowalny. pairs = [(p, y)], y w {0,1}."""
    n = len(pairs)
    if not n:
        return None
    hit = sum(y for _, y in pairs) / n
    z = 1.96
    denom = 1 + z * z / n
    centre = (hit + z * z / (2 * n)) / denom
    half = z * math.sqrt(hit * (1 - hit) / n + z * z / (4 * n * n)) / denom
    num = sum((y - p) * (1 - 2 * p) for p, y in pairs)
    var = sum(((1 - 2 * p) ** 2) * p * (1 - p) for p, y in pairs)
    return {
        "n": n,
        "brier": round(sum((p - y) ** 2 for p, y in pairs) / n, 4),
        "hit_rate": round(hit, 3),
        "hit_ci95": [round(max(0.0, centre - half), 3),
                     round(min(1.0, centre + half), 3)],
        "mean_p": round(sum(p for p, _ in pairs) / n, 3),
        "spiegelhalter_z": round(num / math.sqrt(var), 2) if var > 0 else None,
    }


def _loo_predictions(X_raw, specs, l2, epochs):
    """LOO: kazda obserwacja raz jako test, standaryzacja i fit per fold.
    Zwraca {prog: [(p, etykieta), ...]} dla progow misji."""
    n = len(X_raw)
    out = {th: [] for th in THRESHOLDS}
    for i in range(n):
        Xtr = [X_raw[j] for j in range(n) if j != i]
        str_ = [specs[j] for j in range(n) if j != i]
        Xs, means, stds = _standardize(Xtr)
        cuts = _cutpoints(str_)
        if not cuts:
            continue
        beta, alphas = _fit_ordinal(Xs, str_, cuts, l2, epochs, LR_ORD)
        xi = [(X_raw[i][j] - means[j]) / stds[j] for j in range(len(means))]
        kind, r = specs[i]
        grade_str = (">=" if kind == "censored" else "") + GRADES[r]
        for th in THRESHOLDS:
            lab = label_for(grade_str, th)
            rk = GRADE_RANK[th]
            if lab is None or rk not in alphas:
                continue
            out[th].append((_p_ge(xi, beta, alphas, rk), lab))
    return out


def _choose_l2(X_raw, specs):
    """Lambda z LOO: minimalizacja sumy log-loss na progach misji.
    Mniej epok niz final - ranking lambd jest na to odporny."""
    best, best_ll, report = L2_GRID[0], None, {}
    for l2 in L2_GRID:
        preds = _loo_predictions(X_raw, specs, l2, EPOCHS_TUNE)
        ll = 0.0
        cnt = 0
        for th in THRESHOLDS:
            for p, t in preds[th]:
                ll -= t * math.log(max(p, 1e-13)) + (1 - t) * math.log(max(1 - p, 1e-13))
                cnt += 1
        score = ll / cnt if cnt else float("inf")
        report[str(l2)] = round(score, 4)
        if best_ll is None or score < best_ll:
            best, best_ll = l2, score
    return best, report


# ---------- trening ----------

def mission_projection(goal, mode=None, runs=1000, pool_size=None,
                       max_games=1500, seed=None):
    """Realna projekcja misji: mediana i kwartyle liczby gier do GOAL na
    ktorymkolwiek championie, Z LOSOWANIEM puli (kazda gra: losowe
    pool_size championow, gramy najlepszego wg E(c)). To zastepuje
    "dolna granice" (E lidera), ktora ignorowala, ze lider musi sie
    najpierw trafic. Liczone w tle po treningu, czytane w kafelku.
    runs=1000: przy 300 i seed=None kafelek jitterowal o szum MC po kazdym
    treningu i ruch o kilka gier mogl uchodzic za sygnal (przeglad 2.09,
    W6); regula interpretacji: ruch mediany < ~5 gier to nadal szum.
    Krotnosc szczebla (bonus milestone: S- x2) jak w scoring: sukces
    dopisuje ocene, awans dopiero po skompletowaniu wymogu."""
    import random

    from . import scoring
    from .db import (get_ladder, latest_snapshot_id, median_final_pool_size,
                     snapshot_rows)
    sid = latest_snapshot_id()
    ladder = get_ladder()
    if sid is None or not ladder:
        return None
    rates_all = champion_rates(mode)
    rates, prior = rates_all["champions"], rates_all["prior"]

    def rung(ms):
        return scoring._rung(ladder.get(ms))          # (prog, krotnosc)

    # stan startowy: milestone + oceny juz uzbierane na biezacym szczeblu
    base = {}
    for r in snapshot_rows(sid):
        if r["milestone"] >= goal:
            continue
        grade, _need = rung(r["milestone"])
        earned = json.loads(r.get("grades_earned") or "[]")
        base[r["champion_id"]] = (r["milestone"], scoring._have(earned, grade))
    if len(base) < 2:
        return None
    pool_size = pool_size or median_final_pool_size()
    ids = list(base.keys())
    rnd = random.Random(seed)

    def p_of(cid, ms):
        step = ladder.get(ms)
        if step is None:
            return 0.05
        p, _th, _n = scoring._p_step(cid, step, rates, prior)
        return p or 0.05

    def exp_games(cid, ms, have):
        total = 0.0
        for m in range(ms, goal):
            _grade, need = rung(m)
            left = max(1, need - have) if m == ms else need
            total += left / max(p_of(cid, m), 1e-6)
        return total

    results = []
    for _ in range(runs):
        st = {c: list(v) for c, v in base.items()}    # [milestone, uzbierane]
        games = 0
        done = False
        while games < max_games and not done:
            pool = rnd.sample(ids, min(pool_size, len(ids)))
            best = min(pool, key=lambda c: exp_games(c, st[c][0], st[c][1]))
            games += 1
            if rnd.random() < p_of(best, st[best][0]):
                st[best][1] += 1
                if st[best][1] >= rung(st[best][0])[1]:
                    st[best][0] += 1
                    st[best][1] = 0
                    if st[best][0] >= goal:
                        done = True
        results.append(games if done else max_games)
    results.sort()
    n = len(results)
    return {"median": results[n // 2], "p25": results[n // 4],
            "p75": results[(3 * n) // 4], "runs": runs,
            "pool_size": pool_size,
            "capped": results[-1] >= max_games}


def train(mode=None, save=True, goal=None):
    rows = training_rows(mode)
    baselines, global_median = champion_baselines(mode)
    external = get_json_setting("external_dpm") or None

    X_raw, specs = [], []
    for r in rows:
        spec = _parse_grade(r["grade"])
        if spec is None:
            continue
        f = extract_features(r, baselines, global_median, external, mode)
        X_raw.append([f[k] for k in FEATURES])
        specs.append(spec)

    out = {"trained_at": int(time.time()), "mode": mode,
           "features": FEATURES, "models": {}, "external_used": bool(external),
           "kind": "ordinal"}

    # etykiety per prog - do licznikow, statusow i metryk treningowych
    def labels_for(th):
        y = []
        for kind, r in specs:
            g = (">=" if kind == "censored" else "") + GRADES[r]
            y.append(label_for(g, th))
        return y

    have_core = (len(X_raw) >= 10
                 and len(set(v for v in labels_for(THRESHOLDS[0]) if v is not None)) == 2)
    if not have_core:
        for th in THRESHOLDS:
            y = [v for v in labels_for(th) if v is not None]
            out["models"][th] = {"samples": len(y), "positives": sum(y),
                                 "status": "za malo danych albo brak obu klas"}
        if save:
            set_json_setting("grade_model", out)
        return out

    Xs, means, stds = _standardize(X_raw)
    cuts = _cutpoints(specs)
    l2, l2_report = _choose_l2(X_raw, specs)
    beta, alphas = _fit_ordinal(Xs, specs, cuts, l2, EPOCHS_FINAL, LR_ORD)
    loo = _loo_predictions(X_raw, specs, l2, EPOCHS_VAL)

    out["ordinal"] = {
        "l2": l2, "l2_search": l2_report,
        "trained_on": len(X_raw),
        "cutpoints": {GRADES[c]: round(a, 4) for c, a in alphas.items()},
    }

    for th in THRESHOLDS:
        y_all = labels_for(th)
        y = [v for v in y_all if v is not None]
        pos = sum(y)
        info = {"samples": len(y), "positives": pos,
                "trained_on": len(X_raw), "l2": l2}
        if len(y) < 8 or len(set(y)) < 2:
            info["status"] = "za malo danych albo brak obu klas"
            out["models"][th] = info
            continue
        # ponizej 5 obserwacji ktorejkolwiek klasy metryki tego progu to
        # arytmetyka na garstce - wspolne beta pomaga, ale pewnosci nie daje
        if pos < 5 or (len(y) - pos) < 5:
            info["status"] = "niewiarygodny"
            info["reason"] = f"tylko {pos} pozytywow na {len(y)} obserwacji"

        cv = _threshold_metrics(loo[th])
        info["validation"] = cv
        if cv and not cv["useful"] and not info.get("status"):
            info["status"] = "bez wartosci predykcyjnej"
            info["reason"] = (f"na nowych grach trafia {100*cv['accuracy']:.0f}%, "
                              f"a zgadywanie wiekszosci daje {100*cv['baseline_accuracy']:.0f}%")

        rk = GRADE_RANK[th]
        train_preds = [(_p_ge(x, beta, alphas, rk), lab)
                       for x, lab in zip(Xs, y_all, strict=False) if lab is not None]
        tm = _threshold_metrics(train_preds) or {}
        info.update({
            "status": info.get("status") or
                      ("ok" if len(y) >= MIN_SAMPLES else "poglądowy"),
            "weights": dict(zip(FEATURES, [round(x, 4) for x in beta], strict=False)),
            "bias": round(-alphas[rk], 4),
            "means": means, "stds": stds,
            "metrics": {"accuracy": tm.get("accuracy"),
                        "log_loss": tm.get("log_loss"),
                        "base_rate": tm.get("base_rate")},
        })
        out["models"][th] = info

    if save:
        set_json_setting("grade_model", out)
        set_json_setting("champion_baselines",
                         {"per_champ": baselines, "global": global_median})
        log_event("model_train", {
            "mode": mode, "l2": l2,
            "samples": {t: out["models"][t].get("samples") for t in THRESHOLDS},
        })
        if goal:
            # projekcja misji z losowaniem pul - liczona przy kazdym treningu,
            # zeby kafelek czytal gotowa mediane zamiast dolnej granicy
            try:
                proj = mission_projection(goal, mode)
                if proj:
                    proj["ts"] = int(time.time())
                    set_json_setting("mission_projection", proj)
            except Exception:
                pass
    return out


# ---------- predykcja ----------

def _dmg_percentile(row, mode):
    """(27) Percentyl obrazen/min na tle WSZYSTKICH zaobserwowanych gier
    tym championem w Mayhemie (eog + snowball przez norm_source).
    Drabinka jak w referencji live: champion(>=8) -> klasa(>=8) -> global."""
    from .db import champion_classes, connect
    fv = features.match_features(row)
    dpm = fv["dpm"]
    cid = row["champion_id"]
    clause = "AND m.game_mode = ?" if mode else ""
    args = ["totalDamageDealtToChampions"] + ([mode] if mode else [])
    with connect() as con:
        rows = [(r["champion_id"], r["stat_value"] / (r["duration"] / 60.0))
                for r in con.execute(f"""
            SELECT p.champion_id, p.stat_value, m.duration
            FROM player_stat p JOIN norm_source m ON m.match_id = p.match_id
            WHERE p.stat_key = ? AND m.duration > 300 {clause}""", args)]
    if not rows:
        return None
    MIN_N = 8
    vals = [v for c, v in rows if c == cid]
    scope = "champion"
    if len(vals) < MIN_N:
        classes = champion_classes()
        cls = classes.get(cid)
        clsv = [v for c, v in rows if cls and classes.get(c) == cls]
        if cls and len(clsv) >= MIN_N:
            scope, vals = f"klasa {cls}", clsv
        else:
            scope, vals = "global", [v for _, v in rows]
    below = sum(1 for v in vals if v < dpm)
    return {"stat": "obrazenia/min", "value": round(dpm, 1),
            "pct": round(100.0 * below / len(vals)), "n": len(vals),
            "scope": scope}


def explain(match_id):
    """(13) Karta "czemu taka ocena": wklad kazdej cechy do predykcji
    (waga x z-score; znak = kierunek ciagniecia) dla obu progow
    + percentyl obrazen (27). Model porzadkowy oddaje to za darmo."""
    from .db import connect
    md = get_json_setting("grade_model")
    if not md:
        return None
    with connect() as con:
        r = con.execute("""
            SELECT g.grade, g.champion_id, m.kills, m.deaths, m.assists,
                   m.dmg_champ, m.gold, m.cs, m.vision, m.heal, m.duration,
                   m.game_mode
            FROM grade_observation g JOIN match_player m ON m.match_id = g.match_id
            WHERE g.match_id = ?""", (match_id,)).fetchone()
    if not r:
        return None
    row = dict(r)
    mode = row["game_mode"]
    cached = get_json_setting("champion_baselines") or {}
    baselines = {int(k): v for k, v in (cached.get("per_champ") or {}).items()}
    f = extract_features(row, baselines, cached.get("global") or 1.0, None, mode)
    out = {"match_id": match_id, "grade": row["grade"],
           "champion_id": row["champion_id"], "thresholds": {}}
    for th in ("A-", "S-"):
        m = (md.get("models") or {}).get(th)
        if not m or "weights" not in m:
            continue
        z = m["bias"]
        contribs = []
        for j, key in enumerate(md["features"]):
            c = m["weights"][key] * (f[key] - m["means"][j]) / m["stds"][j]
            z += c
            contribs.append({"feature": key, "value": round(f[key], 2),
                             "pull": round(c, 3)})
        contribs.sort(key=lambda x: -abs(x["pull"]))
        out["thresholds"][th] = {"p": round(_sigmoid(z), 3),
                                 "status": m.get("status"),
                                 "contributions": contribs}
    out["percentile"] = _dmg_percentile(row, mode)
    return out


def predict(row, threshold, model=None, baselines=None, mode=None):
    model = model or get_json_setting("grade_model")
    if not model:
        return None
    m = (model.get("models") or {}).get(threshold)
    if not m or "weights" not in m:
        return None

    if baselines is None:
        cached = get_json_setting("champion_baselines") or {}
        baselines = ({int(k): v for k, v in (cached.get("per_champ") or {}).items()},
                     cached.get("global") or 1.0)

    external = get_json_setting("external_dpm") or None
    f = extract_features(row, baselines[0], baselines[1], external, mode)
    z = m["bias"]
    for j, key in enumerate(model["features"]):
        z += m["weights"][key] * (f[key] - m["means"][j]) / m["stds"][j]
    p = _sigmoid(z)
    quality = m.get("status")
    out = {"p": round(p, 4), "quality": quality, "samples": m.get("samples"),
           "positives": m.get("positives"),
           "features": {k: round(v, 2) for k, v in f.items()}}
    if quality == "niewiarygodny":
        # ciagniemy w strone czestosci bazowej, zeby nie sugerowac pewnosci
        base = (m.get("metrics") or {}).get("base_rate", 0.1)
        out["p_raw"] = round(p, 4)
        out["p"] = round(0.3 * p + 0.7 * base, 4)
        out["warning"] = m.get("reason")
    return out


def readiness():
    """Zamiast wymyslonego progu "40 gier": czy model faktycznie
    przewiduje lepiej niz zgadywanie wiekszosci."""
    m = get_json_setting("grade_model") or {}
    out = {}
    for th, info in (m.get("models") or {}).items():
        cv = info.get("validation")
        out[th] = {
            "samples": info.get("samples"),
            "positives": info.get("positives"),
            "status": info.get("status"),
            "reason": info.get("reason"),
            "validation": cv,
            "verdict": (
                "brak danych" if not cv else
                "działa" if cv["useful"] else
                "nie odróżnia lepiej niż zgadywanie"),
        }
    return out


def champion_rates(mode=None, shrink=6.0):
    """p_A i p_S per champion, sciagniete do sredniej globalnej
    proporcjonalnie do liczby gier. Przy jednej grze wynik bedzie
    prawie rowny sredniej - i tak ma byc."""
    rows = training_rows(mode)
    per = {}
    glob = {th: [0, 0] for th in THRESHOLDS}

    for r in rows:
        for th in THRESHOLDS:
            lab = label_for(r["grade"], th)
            if lab is None:
                continue
            d = per.setdefault(r["champion_id"], {t: [0, 0] for t in THRESHOLDS})
            d[th][0] += lab
            d[th][1] += 1
            glob[th][0] += lab
            glob[th][1] += 1

    prior = {th: (glob[th][0] / glob[th][1]) if glob[th][1] else 0.3 for th in THRESHOLDS}
    out = {}
    for cid, d in per.items():
        out[cid] = {}
        for th in THRESHOLDS:
            hits, n = d[th]
            out[cid][th] = {
                "games": n,
                "raw": round(hits / n, 3) if n else None,
                "shrunk": round((hits + shrink * prior[th]) / (n + shrink), 3),
            }
    return {"prior": {k: round(v, 3) for k, v in prior.items()}, "champions": out}


# ============================================================
#  Walidacja krzyzowa i odwracanie modelu na progi
# ============================================================

# Cechy, ktore da sie kontrolowac w grze. duration_min zostaje
# w modelu jako korekta, ale nie jest celem do trafienia.
ACTIONABLE = ["gold_per_min", "ka_per_min", "deaths_per_min", "dmg_ratio"]

# Kierunek: czy wieksza wartosc pomaga
HIGHER_IS_BETTER = {
    "gold_per_min": True, "ka_per_min": True,
    "deaths_per_min": False, "dmg_ratio": True, "duration_min": True,
}

FEATURE_LABELS = {
    "gold_per_min": "złoto na minutę",
    "ka_per_min": "zabójstwa + asysty na minutę",
    "deaths_per_min": "zgony na minutę",
    "dmg_ratio": "obrażenia na minutę",
    "duration_min": "długość gry",
}



def champion_medians(champion_id, mode=None):
    """Typowe wartosci cech na tym championie. Fallback: wszystkie mecze."""
    clause = "AND game_mode = ?" if mode else ""
    args_c = ([champion_id, mode] if mode else [champion_id])
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            f"""SELECT kills, deaths, assists, dmg_champ, gold, duration
                FROM match_player WHERE champion_id = ? AND duration > 300 {clause}""",
            args_c)]
        if len(rows) < 2:
            rows = [dict(r) for r in con.execute(
                f"""SELECT kills, deaths, assists, dmg_champ, gold, duration
                    FROM match_player WHERE duration > 300 {clause}""",
                ([mode] if mode else []))]
    if not rows:
        return None, 0

    baselines, gmed = champion_baselines(mode)
    feats = [extract_features(r, baselines, gmed, None, mode) for r in rows]
    med = {k: statistics.median(f[k] for f in feats) for k in FEATURES}
    return med, len(rows)


def _observed_range(key, mode=None, dmg_base=None):
    """Zakres, w ktorym kiedykolwiek sie znalazles - poza nim cel jest
    ekstrapolacja, a nie wskazowka."""
    clause = "AND game_mode = ?" if mode else ""
    args = ([mode] if mode else [])
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            f"""SELECT kills, deaths, assists, dmg_champ, gold, duration
                FROM match_player WHERE duration > 300 {clause}""", args)]
    if not rows:
        return None
    baselines, gmed = champion_baselines(mode)
    vals = []
    for r in rows:
        f = extract_features(r, baselines, gmed, None, mode)
        v = f[key]
        if key == "dmg_ratio" and dmg_base:
            v *= dmg_base
        vals.append(v)
    return (min(vals), max(vals))


def targets_for(champion_id, threshold, mode=None, model_data=None):
    """Odwraca model: jakie wartosci daja 50% szans na dana ocene.

    Dla kazdej cechy liczymy, gdzie musialaby byc, gdyby reszta zostala
    na Twoim typowym poziomie na tym championie. To odpowiedz na pytanie
    "co mam zrobic", a nie "ile gier mi to zajmie"."""
    model_data = model_data or get_json_setting("grade_model")
    if not model_data:
        return None
    m = (model_data.get("models") or {}).get(threshold)
    if not m or "weights" not in m:
        return None

    # Model, ktory nie przechodzi walidacji, nie ma prawa mowic "celuj w X".
    # Odwrocenie zlego modelu daje rady w rodzaju "gin czesciej".
    cv = m.get("validation")
    if m.get("status") in ("niewiarygodny", "bez wartosci predykcyjnej") or \
       (cv and not cv.get("useful")):
        return {
            "champion_id": champion_id,
            "threshold": threshold,
            "unavailable": True,
            "reason": m.get("reason") or "model nie przewiduje lepiej niz zgadywanie",
            "samples": m.get("samples"),
            "positives": m.get("positives"),
            "targets": [],
        }

    med, n_games = champion_medians(champion_id, mode)
    if not med:
        return None

    feats = model_data["features"]
    means, stds = m["means"], m["stds"]
    w = m["weights"]

    def z_of(vals):
        return m["bias"] + sum(
            w[k] * (vals[k] - means[j]) / stds[j] for j, k in enumerate(feats))

    baselines, gmed = champion_baselines(mode)
    dmg_base = baselines.get(champion_id) or gmed

    out = []
    for key in ACTIONABLE:
        if key not in feats:
            continue
        j = feats.index(key)
        wj = w[key]
        if abs(wj) < 1e-6:
            continue

        # z bez tej cechy
        rest = m["bias"] + sum(
            w[k] * (med[k] - means[i]) / stds[i]
            for i, k in enumerate(feats) if k != key)
        # rozwiazujemy rest + wj*(x-mu)/sd = 0
        x = means[j] + stds[j] * (-rest / wj)

        current = med[key]
        # dmg_ratio prezentujemy jako obrazenia na minute
        if key == "dmg_ratio":
            x_show, cur_show = x * dmg_base, current * dmg_base
        else:
            x_show, cur_show = x, current

        higher = HIGHER_IS_BETTER[key]

        # Waga o znaku przeciwnym do kierunku cechy to szum, nie odkrycie.
        # "Gin czesciej, dostaniesz S-" nie jest hipoteza wartą pokazania.
        if (wj > 0) != higher:
            continue

        # cel poza tym, co kiedykolwiek osiagnales, to ekstrapolacja
        obs = _observed_range(key, mode, dmg_base)
        beyond = bool(obs and (x_show > obs[1] if higher else x_show < obs[0]))

        out.append({
            "beyond_observed": beyond,
            "observed_max": round(obs[1], 1) if obs else None,
            "feature": key,
            "label": FEATURE_LABELS[key],
            "target": round(x_show, 1),
            "current": round(cur_show, 1),
            "gap": round(x_show - cur_show, 1),
            "higher_is_better": higher,
            "reachable": bool(not beyond and x_show > 0),
            "weight": wj,
        })

    p_now = _sigmoid(z_of(med))

    # Nierealne cele odpadaja: ujemne wartosci i te poza tym,
    # co kiedykolwiek osiagnales.
    usable = [t for t in out if t["reachable"]]

    # Najtansza dzwignia: najmniejszy wzgledny ruch od stanu obecnego.
    # Jedna oś, bo w grze i tak nie zoptymalizujesz czterech naraz.
    for t in usable:
        base = abs(t["current"]) or 1.0
        t["relative_effort"] = round(abs(t["gap"]) / base, 3)
    usable.sort(key=lambda t: t["relative_effort"])

    # Dzwignia ma sens tylko wtedy, gdy faktycznie wskazuje kierunek:
    # cel po wlasciwej stronie stanu obecnego i ruch niepomijalny.
    # Dzwignia wylaczona swiadomie. Przy tej probce mediany per champion
    # opieraja sie na 1-5 grach albo na fallbacku globalnym, a model ma
    # jedna dominujaca ceche - wiec "prog do trafienia" wychodzil albo
    # rowny stanowi obecnemu, albo poza zasiegiem. Sama szansa `p` jest
    # zwalidowana (AUC 0.78) i roznicuje championow, wiec to ona jest
    # przekazem. Wrocic, gdy bedzie po kilkanascie gier na postac.
    lever = None
    for t in usable:
        moves_right_way = (t["gap"] > 0) == t["higher_is_better"]
        meaningful = t["relative_effort"] >= 0.15 and abs(t["gap"]) >= 1
        if moves_right_way and meaningful and n_games >= 8:
            lever = t
            break
    return {
        "champion_id": champion_id,
        "threshold": threshold,
        "based_on_games": n_games,
        "p_at_current": round(p_now, 4),
        "verdict": (
            "blisko" if p_now >= 0.40 else
            "daleko" if p_now >= 0.15 else
            "bardzo daleko"),
        "lever": lever,
        "lever_note": (
            None if lever else
            f"za malo gier na tym championie ({n_games}), zeby wyznaczyc prog - "
            "patrz na sama szanse"),
        "targets": usable,
        "rejected": [t["label"] for t in out if not t["reachable"]],
    }


def own_games_map(mode=None):
    """Liczba wlasnych gier per champion. To jest realny roznicownik miedzy
    championami stojacymi na tym samym szczeblu - byl w sortowaniu, ale nie
    byl pokazywany."""
    where = "WHERE duration > 300" + (" AND game_mode = ?" if mode else "")
    args = (mode,) if mode else ()
    with connect() as con:
        return {r["champion_id"]: r["n"] for r in con.execute(
            f"SELECT champion_id, COUNT(*) n FROM match_player {where} "
            f"GROUP BY champion_id", args)}
