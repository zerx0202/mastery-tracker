"""
Model prawdopodobienstwa oceny pomeczowej.

Dwa niezalezne klasyfikatory progowe:
  P(ocena >= A-)  - potrzebne na milestone 0->1 i 1->2
  P(ocena >= S-)  - potrzebne na milestone 2->3 i 3->4

Dlaczego progowe, a nie jedna skala: 19 z 35 obserwacji jest cenzurowanych.
Awans milestone'a mowi ">= A-", ale nie mowi, czy bylo S-. Taka obserwacja
jest pelnowartościowa dla modelu A-, a dla modelu S- musi zostac pominieta,
inaczej zafalszuje wynik.

Dlaczego obrazenia sa normalizowane przez championa: w danych B+ ma
NIZSZE obrazenia na minute niz C, bo B+ to postacie utility. Ocena jest
liczona wzgledem innych grajacych ta postacia, wiec surowa wartosc jest
mylaca. gold/min jest za to monotoniczne i nie wymaga normalizacji.
"""

import json
import math
import statistics
import time

from .db import GRADE_RANK, connect, get_json_setting, log_event, set_json_setting

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
TUNING_TARGET = 40      # marker: od tylu obserwacji warto stroic
L2 = 1.0
EPOCHS = 3000
LR = 0.08


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


def extract_features(row, baselines, global_median, external=None):
    """row: mecz z match_player. external: {champion_id: srednia_dpm} ze zrodla
    zewnetrznego, gdy bedzie dostepne - ma pierwszenstwo przed wlasna mediana."""
    mins = max((row.get("duration") or 0) / 60, 1.0)
    dpm = (row.get("dmg_champ") or 0) / mins

    base = None
    if external:
        base = external.get(row.get("champion_id"))
    if base is None:
        own = baselines.get(row.get("champion_id"))
        # wlasna mediana ma sens dopiero przy kilku grach na postaci
        base = own if own and own > 0 else global_median

    return {
        "gold_per_min": (row.get("gold") or 0) / mins,
        "ka_per_min": ((row.get("kills") or 0) + (row.get("assists") or 0)) / mins,
        "deaths_per_min": (row.get("deaths") or 0) / mins,
        "dmg_ratio": dpm / base if base else 1.0,
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


# ---------- regresja logistyczna ----------

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


def _fit(X, y):
    """Regresja logistyczna z L2, prosty gradient. Przy 35 probkach
    i 5 cechach nie ma sensu ciagnac zaleznosci typu scikit-learn."""
    n, k = len(X), len(X[0])
    w = [0.0] * k
    b = 0.0
    for _ in range(EPOCHS):
        gw = [0.0] * k
        gb = 0.0
        for xi, yi in zip(X, y):
            p = _sigmoid(sum(w[j] * xi[j] for j in range(k)) + b)
            err = p - yi
            for j in range(k):
                gw[j] += err * xi[j]
            gb += err
        for j in range(k):
            w[j] = w[j] - LR * (gw[j] / n + L2 * w[j] / n)
        b -= LR * gb / n
    return w, b


def _metrics(X, y, w, b):
    correct = 0
    ll = 0.0
    for xi, yi in zip(X, y):
        p = _sigmoid(sum(w[j] * xi[j] for j in range(len(w))) + b)
        correct += int((p >= 0.5) == bool(yi))
        ll += yi * math.log(max(p, 1e-13)) + (1 - yi) * math.log(max(1 - p, 1e-13))
    base = sum(y) / len(y)
    return {
        "accuracy": round(correct / len(y), 3),
        "log_loss": round(-ll / len(y), 4),
        "base_rate": round(base, 3),
    }


# ---------- trening ----------

def train(mode=None, save=True):
    rows = training_rows(mode)
    baselines, global_median = champion_baselines(mode)
    external = get_json_setting("external_dpm") or None

    out = {"trained_at": int(time.time()), "mode": mode,
           "features": FEATURES, "models": {}, "external_used": bool(external)}

    for th in THRESHOLDS:
        X_raw, y = [], []
        for r in rows:
            lab = label_for(r["grade"], th)
            if lab is None:
                continue
            f = extract_features(r, baselines, global_median, external)
            X_raw.append([f[k] for k in FEATURES])
            y.append(lab)

        pos = sum(y)
        info = {"samples": len(y), "positives": pos}
        if len(y) < 8 or len(set(y)) < 2:
            info["status"] = "za malo danych albo brak obu klas"
            out["models"][th] = info
            continue
        # przy garstce pozytywow model uczy sie mowic "nie" na wszystko
        # i raportuje 100% trafnosci - to arytmetyka, nie umiejetnosc
        if pos < 5 or (len(y) - pos) < 5:
            info["status"] = "niewiarygodny"
            info["reason"] = f"tylko {pos} pozytywow na {len(y)} obserwacji"

        X, means, stds = _standardize(X_raw)
        w, b = _fit(X, y)
        cv = _cross_validate(X_raw, y)
        info["validation"] = cv
        if cv and not cv["useful"] and not info.get("status"):
            info["status"] = "bez wartosci predykcyjnej"
            info["reason"] = (f"na nowych grach trafia {100*cv['accuracy']:.0f}%, "
                              f"a zgadywanie wiekszosci daje {100*cv['baseline_accuracy']:.0f}%")
        info.update({
            "status": info.get("status") or
                      ("ok" if len(y) >= MIN_SAMPLES else "poglądowy"),
            "weights": dict(zip(FEATURES, [round(x, 4) for x in w])),
            "bias": round(b, 4),
            "means": means, "stds": stds,
            "metrics": _metrics(X, y, w, b),
        })
        out["models"][th] = info

    if save:
        set_json_setting("grade_model", out)
        set_json_setting("champion_baselines",
                         {"per_champ": baselines, "global": global_median})
        log_event("model_train", {
            "mode": mode,
            "samples": {t: out["models"][t].get("samples") for t in THRESHOLDS},
        })
    return out


# ---------- predykcja ----------

def predict(row, threshold, model=None, baselines=None):
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
    f = extract_features(row, baselines[0], baselines[1], external)
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


def _cross_validate(X_raw, y, folds=None):
    """Leave-one-out przy malej probce: kazda obserwacja raz jako test.
    To jedyna uczciwa miara - metryki na danych treningowych zawsze
    wygladaja lepiej, niz model faktycznie dziala."""
    n = len(y)
    if n < 10 or len(set(y)) < 2:
        return None
    folds = folds or n

    preds = []
    for i in range(n):
        Xtr = [X_raw[j] for j in range(n) if j != i]
        ytr = [y[j] for j in range(n) if j != i]
        if len(set(ytr)) < 2:
            continue
        Xs, means, stds = _standardize(Xtr)
        w, b = _fit(Xs, ytr)
        xi = [(X_raw[i][k] - means[k]) / stds[k] for k in range(len(means))]
        p = _sigmoid(sum(w[k] * xi[k] for k in range(len(w))) + b)
        preds.append((p, y[i]))

    if not preds:
        return None

    correct = sum(1 for p, t in preds if (p >= 0.5) == bool(t))
    ll = sum(t * math.log(max(p, 1e-13)) + (1 - t) * math.log(max(1 - p, 1e-13))
             for p, t in preds)
    base = sum(y) / n
    base_acc = max(base, 1 - base)

    # AUC: udzial par (pozytyw, negatyw) uszeregowanych poprawnie
    pos = [p for p, t in preds if t == 1]
    neg = [p for p, t in preds if t == 0]
    auc = None
    if pos and neg:
        wins = sum(1 for a in pos for b_ in neg if a > b_)
        ties = sum(1 for a in pos for b_ in neg if a == b_)
        auc = (wins + 0.5 * ties) / (len(pos) * len(neg))

    acc = correct / len(preds)
    return {
        "method": "leave-one-out",
        "tested": len(preds),
        "accuracy": round(acc, 3),
        "baseline_accuracy": round(base_acc, 3),
        "lift": round(acc - base_acc, 3),
        "auc": round(auc, 3) if auc is not None else None,
        "log_loss": round(-ll / len(preds), 4),
        "useful": bool(auc is not None and auc >= 0.65 and acc > base_acc),
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
    feats = [extract_features(r, baselines, gmed) for r in rows]
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
        f = extract_features(r, baselines, gmed)
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
