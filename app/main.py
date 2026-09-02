import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles

from . import balance, db, features, model, scoring
from .db import GRADE_RANK
from .limiter import RateLimiter

API_KEY = os.environ["RIOT_API_KEY"]
PLATFORM = os.environ.get("RIOT_PLATFORM", "euw1")
REGION = os.environ.get("RIOT_REGION", "europe")
MY_NAME = os.environ["MY_RIOT_NAME"]
MY_TAG = os.environ["MY_RIOT_TAG"]
GOAL = int(os.environ.get("GOAL_MILESTONE", "4"))
DEFAULT_MODE = os.environ.get("DEFAULT_MODE") or None
EXCLUDED_MODES = tuple(
    m.strip() for m in os.environ.get("EXCLUDED_MODES", "JADE").split(",") if m.strip()
)

state = {}


def norm(x: str) -> str:
    """Luzne dopasowanie nazw: Dr. Mundo == drmundo == dr mundo."""
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


async def mayhem_sentinel_loop():
    """Riot dzis celowo blokuje Mayhema w match-v5 (403; developer-relations
    #1109 i #1154). Raz na dobe sprawdzamy jednym zapytaniem, czy to sie
    zmienilo - w dniu otwarcia dostajemy baner i mozliwosc backfillu
    pelnych danych z timeline'ami."""
    await asyncio.sleep(30)
    while True:
        try:
            st = db.get_json_setting("mayhem_api") or {}
            if time.time() - st.get("checked_at", 0) >= 20 * 3600:
                region = os.getenv("REGION", "europe")
                puuid = await my_puuid()
                ids = []
                try:
                    ids = await riot_get(
                        f"https://{region}.api.riotgames.com/lol/match/v5/"
                        f"matches/by-puuid/{puuid}/ids",
                        params={"queue": 2400, "count": 1}) or []
                except HTTPException:
                    ids = []  # 403/404 = nadal zablokowane, to norma
                db.set_json_setting("mayhem_api", {
                    "checked_at": int(time.time()),
                    "open": bool(ids),
                    "sample": (ids or [None])[0]})
                if ids:
                    db.log_event("mayhem_api_open", {"match_id": ids[0]},
                                 int(time.time()))
        except Exception:
            pass
        await asyncio.sleep(3600)


async def daily_snapshot_loop():
    """Karta 17: snapshoty bez klienta. Endpoint /snapshot liczy z publicznego
    champion-mastery-v4 (agent tylko go wyzwala), wiec w dni bez Windowsa
    nikt nie strzela - a milestoneGrades zeruje sie przy awansie, czyli
    dziura w zbieraniu jest realna. Raz na godzine sprawdzamy: ostatni
    snapshot starszy niz 20 h -> robimy wlasny, z osobnym wpisem w dzienniku."""
    await asyncio.sleep(90)
    while True:
        try:
            last = await asyncio.to_thread(db.latest_snapshot_ts)
            if time.time() - (last or 0) >= 20 * 3600:
                r = await snapshot()
                await asyncio.to_thread(
                    db.log_event, "snapshot_cron",
                    {"snapshot_id": r.get("snapshot_id"),
                     "champions": r.get("champions")}, int(time.time()))
        except Exception:
            pass
        await asyncio.sleep(3600)


async def balance_refresh_loop():
    """(48) Mnozniki balansu Mayhema per champion - odswiezane raz na dobe,
    bo zmieniaja sie tylko z patchem (i hotfixami). Zrodlo, parser
    i decyzja o granicach uzycia: app/balance.py."""
    await asyncio.sleep(120)
    while True:
        try:
            st = db.get_json_setting("mayhem_balance") or {}
            if time.time() - st.get("fetched_at", 0) >= 24 * 3600:
                r = await state["plain"].get(balance.BALANCE_URL,
                                             follow_redirects=True)
                if r.status_code == 200:
                    await asyncio.to_thread(balance.store_balance, r.text)
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # jeden punkt wejscia do schematu - nowa funkcja init_*/upgrade_* w db.py
    # wykona sie sama, bez pamietania o tej liscie (to tu ginely nowe wpisy)
    db.migrate()
    asyncio.create_task(mayhem_sentinel_loop())
    asyncio.create_task(daily_snapshot_loop())
    asyncio.create_task(balance_refresh_loop())
    state["limiter"] = RateLimiter()
    state["sync"] = {"running": False, "done": 0, "total": 0, "msg": "nie uruchomiony"}
    state["client"] = httpx.AsyncClient(headers={"X-Riot-Token": API_KEY}, timeout=15.0)
    state["plain"] = httpx.AsyncClient(timeout=15.0)
    yield
    await state["client"].aclose()
    await state["plain"].aclose()


app = FastAPI(title="Mastery Tracker", lifespan=lifespan)
API_TOKEN = os.getenv("API_TOKEN", "").strip()


def require_token(x_api_token: str | None = Header(default=None)):
    """Pusty token = brak ochrony (tryb domowy). Ustawiony w .env wymusza
    naglowek X-API-Token na endpointach zapisujacych."""
    if not API_TOKEN:
        return
    if x_api_token != API_TOKEN:
        raise HTTPException(401, "zly lub brakujacy token")


api = APIRouter(prefix="/api")
write_api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


async def riot_get(url, params=None, attempts=4):
    """Limiter czyta naglowki Riota, wiec czekamy tylko gdy naprawde trzeba.
    Przy 429 i bledach 5xx wycofujemy sie wykladniczo zamiast bic w sciane."""
    delay = db.LIMITER.delay()
    if delay > 0:
        await asyncio.sleep(min(delay, 20))

    backoff = 1.0
    last = None
    async with httpx.AsyncClient() as client:
        for attempt in range(attempts):
            r = await client.get(url, params=params,
                                 headers={"X-Riot-Token": API_KEY}, timeout=20)
            db.LIMITER.note(r.headers)

            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None

            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                if attempt < attempts - 1:
                    wait = backoff
                    ra = r.headers.get("Retry-After")
                    if ra:
                        try:
                            wait = max(wait, float(ra))
                        except ValueError:
                            pass
                    await asyncio.sleep(min(wait, 30))
                    backoff *= 2
                    continue

            raise HTTPException(r.status_code, f"Riot API: {r.text[:200]}")

    raise HTTPException(503, f"Riot API nie odpowiada ({last})")


async def my_puuid():
    riot_id = f"{MY_NAME}#{MY_TAG}"
    if cached := db.get_cached_puuid(riot_id):
        return cached
    data = await riot_get(
        f"https://{REGION}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{quote(MY_NAME)}/{quote(MY_TAG)}")
    db.cache_puuid(riot_id, data["puuid"], int(time.time()))
    return data["puuid"]


# ---------------- podstawy ----------------

@api.get("/health")
async def health():
    return {
        "status": "ok",
        "platform": PLATFORM,
        "champions_cached": db.champion_count(),
        "goal": GOAL,
        "default_mode": DEFAULT_MODE,
        "excluded_modes": list(EXCLUDED_MODES),
    }


@write_api.post("/refresh-champions")
async def refresh_champions(force: bool = False):
    """Data Dragon - nazwy i klucze ikon. Bez klucza API, bez limitow."""
    vers = (await state["plain"].get(
        "https://ddragon.leagueoflegends.com/api/versions.json")).json()
    patch = vers[0]
    if not force and db.get_setting("ddragon_patch") == patch and db.champion_count() > 0:
        return {"patch": patch, "champions": db.champion_count(), "skipped": True}
    data = (await state["plain"].get(
        f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json")).json()
    champs = [(int(v["key"]), v["name"], v["id"], ",".join(v.get("tags") or []))
              for v in data["data"].values()]
    db.save_champions(champs)
    db.set_setting("ddragon_patch", patch)
    db.log_event("ddragon", {"patch": patch, "champions": len(champs)})
    return {"patch": patch, "champions": len(champs)}


@write_api.post("/snapshot")
async def snapshot():
    puuid = await my_puuid()
    data = await riot_get(
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}")
    ts = int(time.time())
    prev = db.latest_snapshot_id()
    sid = await asyncio.to_thread(db.save_snapshot, ts, data)
    new_split = await asyncio.to_thread(db.detect_split_reset, prev, sid, ts)
    await asyncio.to_thread(db.learn_ladder, data, ts)
    await asyncio.to_thread(db.log_event, "snapshot",
                            {"snapshot_id": sid, "champions": len(data)}, ts)
    out = {"snapshot_id": sid, "taken_at": ts, "champions": len(data)}
    if new_split:
        out["split_reset"] = new_split
    return out


@api.get("/ladder")
async def ladder():
    known = db.get_ladder()
    return {"known": known, "missing": [m for m in range(GOAL) if m not in known], "goal": GOAL}


# ---------------- scoring ----------------

def cheapest_grade(req):
    if not req:
        return None
    return min(req.keys(), key=lambda g: GRADE_RANK.get(g, 99))


def path_to_goal(current, ladder):
    games, steps, marks = 0, [], 0
    for ms in range(current, GOAL):
        step = ladder.get(ms)
        if step is None:
            return None, steps, marks
        games += step["games"]
        marks += step["reward_marks"]
        steps.append({"from": ms, "to": ms + 1,
                      "grade": cheapest_grade(step["require_grades"]),
                      "games": step["games"]})
    return games, steps, marks


def patch_meta(mode=None):
    """Baner patch-awareness (karta 15): normy i model licza sie glownie
    na poprzednim patchu, a patch w Mayhemie zmienia tez mnozniki balansu
    trybu (potwierdzone na customach, 1.09)."""
    cur = db.get_setting("ddragon_patch") or ""
    short = ".".join(cur.split(".")[:2]) if cur else None
    games = db.games_on_patch(short, mode) if short else 0
    return {"version": cur or None, "short": short, "games": games,
            "fresh": bool(short) and games < 8}


@api.get("/targets")
async def targets(limit: int = 30, only: str | None = None,
                  ids: str | None = None, mode: str | None = None):
    sid = db.latest_snapshot_id()
    if sid is None:
        raise HTTPException(400, "Brak snapshotow - zrob POST /snapshot")

    ladder = db.get_ladder()
    use_mode = mode or DEFAULT_MODE

    wanted_ids = None
    if ids:
        wanted_ids = {int(x) for x in ids.split(",") if x.strip()}
    wanted = None
    if only:
        wanted = {norm(s) for s in only.split(",") if s.strip()}

    rates_all = await asyncio.to_thread(model.champion_rates, use_mode)
    rates, prior = rates_all["champions"], rates_all["prior"]

    out = []
    for r in db.snapshot_rows(sid):
        name = r["name"] or str(r["champion_id"])
        if wanted_ids is not None and r["champion_id"] not in wanted_ids:
            continue
        if wanted and norm(name) not in wanted and norm(r["key"] or "") not in wanted:
            continue
        if r["milestone"] >= GOAL:
            continue

        out.append({
            "champion_id": r["champion_id"],
            "name": name,
            "key": r["key"],
            "milestone": r["milestone"],
            "next_games": r["next_games"],
            "grades_earned": json.loads(r["grades_earned"] or "[]"),
            "level": r["level"],
            "points": r["points"],
            "tokens": r["tokens"],
            "last_play": r["last_play"],
        })

    scoring.score_rows(out, ladder, rates, prior, GOAL)

    # progi "co trafic" dla kilku pierwszych - to jest odpowiedz na pytanie
    # "co mam zrobic", ktorej sama liczba gier nie daje
    md = db.get_json_setting("grade_model")
    ready = await asyncio.to_thread(model.readiness)
    own_counts = await asyncio.to_thread(model.own_games_map, use_mode)
    pop = await asyncio.to_thread(db.champion_sb_popularity)
    nz = sorted(v for v in pop.values() if v > 0)

    def pop_tier(n):
        if not n or len(nz) < 3:
            return None
        lo, hi = nz[len(nz) // 3], nz[(2 * len(nz)) // 3]
        return "rzadki" if n <= lo else ("czesty" if n >= hi else "sredni")

    for r in out:
        r["model_own_games"] = own_counts.get(r["champion_id"], 0)
        n_pop = pop.get(r["champion_id"], 0)
        r["sb_pop"] = n_pop
        r["pop_tier"] = pop_tier(n_pop)
        # znacznik eksploracji (karta 33): gra ma tez wartosc informacyjna
        r["explore"] = r["model_own_games"] < 3
    for r in out[:limit]:
        th = r.get("next_threshold")
        if not th:
            continue
        aim = await asyncio.to_thread(
            model.targets_for, r["champion_id"], th, use_mode, md)
        r["aim"] = aim
        # szansa z modelu jest wiarygodna tylko dla progow, ktore przeszly walidacje
        v = (ready.get(th) or {}).get("validation") or {}
        r["model_p"] = None if (not aim or aim.get("unavailable")) else aim.get("p_at_current")
        r["model_verdict"] = (ready.get(th) or {}).get("verdict")
        r["model_auc"] = v.get("auc")
        r["model_games"] = None if not aim else aim.get("based_on_games")
    state["last_best_expected"] = out[0]["expected_games"] if out else None
    return {
        "goal": GOAL,
        "snapshot_id": sid,
        "mode": use_mode,
        "prior": prior,
        "summary": scoring.summarize(out, GOAL),
        "patch": await asyncio.to_thread(patch_meta, use_mode),
        "targets": out[:limit],
    }


@api.get("/weights")
async def get_weights():
    """Zachowane dla zgodnosci ze starym frontendem. Scoring nie uzywa juz
    wag - liczy oczekiwana liczbe gier z modelu."""
    return {"weights": {}, "defaults": {},
            "note": "scoring liczy oczekiwana liczbe gier, wagi sa nieaktualne"}


# ---------------- lobby ----------------

@write_api.post("/lobby")
async def push_lobby(payload: dict):
    ids = [int(x) for x in payload.get("champion_ids", [])]
    trade = [int(x) for x in payload.get("trade_ids", []) if x]
    ts = int(time.time())
    db.set_lobby(ids, payload.get("queue"), payload.get("pool_kind"), ts, trade)
    state["last_queue_id"] = payload.get("queue_id")

    # historia pul: bez tego nie wiadomo, jaki mial byc wybor
    pool_id = None
    if ids:
        pool_id = await asyncio.to_thread(
            db.save_pool, ids, payload.get("queue"), payload.get("queue_id"),
            payload.get("pool_kind"), ts, trade)
        # agent wysyla kazda rotacje z lawka (ta sama unia -> ten sam pool_id);
        # event i predykcje sa per pula, nie per rotacja - inaczej event_log
        # puchnie, a predykcje sa liczone w kolko dla identycznych ids
        if pool_id and pool_id != state.get("last_pool_id"):
            state["last_pool_id"] = pool_id
            await asyncio.to_thread(db.log_event, "champ_select",
                                    {"pool_id": pool_id, "size": len(ids),
                                     "queue": payload.get("queue")}, ts)
            # predykcje PRZED gra - jedyny test modelu, ktorego nie da sie
            # oszukac; wynik doklei sie sam przez link_pool_to_match
            try:
                t = await targets(limit=200, ids=",".join(map(str, ids)),
                                  mode=payload.get("queue"))
                n = await asyncio.to_thread(
                    db.save_pool_predictions, pool_id, t.get("targets", []), ts)
                state["last_predictions"] = n
            except Exception as e:
                await asyncio.to_thread(db.log_event, "prediction_error",
                                        {"err": str(e)[:200]}, ts)
    return {"ok": True, "count": len(ids), "pool_id": pool_id,
            "pool_kind": payload.get("pool_kind")}


@api.get("/lobby")
async def read_lobby(max_age: int = 5400):
    lob = db.get_lobby()
    if not lob or not lob["champion_ids"]:
        return {"active": False, "targets": []}
    age = int(time.time()) - lob["updated_at"]
    if age > max_age:
        return {"active": False, "age": age, "targets": []}
    ids = ",".join(str(i) for i in lob["champion_ids"])
    t = await targets(limit=200, ids=ids, mode=lob["queue"])
    return {
        "active": True,
        "age": age,
        "queue": lob["queue"],
        "pool_kind": lob["pool_kind"],
        "champion_ids": lob["champion_ids"],
        "trade_ids": lob.get("trade_ids") or [],
        "prior": t.get("prior"),
        "summary": t.get("summary"),
        "patch": t.get("patch"),
        "targets": t["targets"],
    }


# ---------------- snapshoty i postep ----------------

@api.get("/snapshots")
async def snapshots():
    return db.list_snapshots()


@api.get("/progress")
async def progress(from_id: int | None = None, to_id: int | None = None):
    snaps = db.list_snapshots()
    if len(snaps) < 2 and (from_id is None or to_id is None):
        raise HTTPException(400, "Potrzebne co najmniej dwa snapshoty")
    to_id = to_id or snaps[0]["id"]
    from_id = from_id or snaps[1]["id"]
    rows = db.diff(from_id, to_id)
    return {"from_id": from_id, "to_id": to_id,
            "total_gained": sum(r["gained"] or 0 for r in rows),
            "champions": rows}


# ---------------- historia meczow ----------------

async def match_ids_page(puuid, start, count=100, after=None):
    url = (f"https://{REGION}.api.riotgames.com"
           f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start={start}&count={count}")
    if after:
        url += f"&startTime={int(after)}"
    return await riot_get(url)


async def sync_worker():
    sync = state["sync"]
    try:
        puuid = await my_puuid()
        ts = int(time.time())

        full = sync.get("full", False)
        after = None
        if not full:
            last = db.latest_game_creation()
            if last:
                after = last // 1000 - 3600

        sync["msg"] = "przyrostowo" if after else "pelna historia"
        start, new_total = 0, 0
        while True:
            page = await match_ids_page(puuid, start, after=after)
            if not page:
                break
            added = await asyncio.to_thread(db.add_match_ids, page, ts)
            new_total += added
            start += len(page)
            sync["msg"] = f"zebrano {start} ID ({new_total} nowych)"
            if len(page) < 100:
                break
            if not full and added == 0:
                break

        todo = db.pending_match_ids()
        sync["total"] = len(todo)
        sync["done"] = 0
        for mid in todo:
            if not sync["running"]:
                sync["msg"] = "zatrzymane"
                return
            try:
                data = await riot_get(
                    f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{mid}")
                await asyncio.to_thread(db.save_match, mid, data["info"], puuid)
            except Exception:
                await asyncio.to_thread(db.mark_failed, mid)
            sync["done"] += 1
            sync["msg"] = f"pobrano {sync['done']}/{sync['total']}"

        sync["msg"] = f"gotowe: {sync['done']} meczow"
    except Exception as e:
        sync["msg"] = f"blad: {type(e).__name__}: {e}"
    finally:
        sync["running"] = False


@write_api.post("/history/sync")
async def history_sync(full: bool = False):
    sync = state["sync"]
    if sync["running"]:
        return {"already_running": True, **sync}
    sync.update({"running": True, "done": 0, "total": 0, "msg": "startuje", "full": full})
    asyncio.create_task(sync_worker())
    return {"started": True}


@write_api.post("/history/stop")
async def history_stop():
    state["sync"]["running"] = False
    return {"stopping": True}


@api.get("/history/status")
async def history_status():
    return {**state["sync"], **db.history_stats()}


@write_api.post("/history/lcu")
async def history_lcu(payload: dict):
    """Agent wysyla tu surowe gry z historii LCU."""
    games = payload.get("games") or []
    new = 0
    errors = []
    for g in games:
        try:
            if await asyncio.to_thread(db.save_lcu_game, g):
                new += 1
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    if games:
        await asyncio.to_thread(db.log_event, "history_lcu",
                                {"received": len(games), "new": new})
    if new:
        # Trening po ocenie strzela ZA WCZESNIE w potoku: grade laduje
        # ~30 s przed wierszem match_player (ten powstaje dopiero tutaj),
        # a training_rows JOIN-uje oba - swieza ocena wchodzila do modelu
        # dopiero przy nastepnej grze. Drugi trigger domyka potok; trening
        # jest idempotentny, wiec podwojne odpalenie kosztuje tylko CPU w tle.
        try:
            await asyncio.to_thread(model.train, DEFAULT_MODE, True, GOAL)
        except Exception as e:
            await asyncio.to_thread(db.log_event, "model_train_fail",
                                    {"error": f"{type(e).__name__}: {e}"},
                                    int(time.time()))
    return {"received": len(games), "new": new, "errors": errors[:5]}


@api.get("/history/modes")
async def history_modes():
    return db.mode_breakdown()


@write_api.post("/grade")
async def push_grade(payload: dict):
    """Agent wysyla tu ocene pomeczowa z LCU (champion-mastery-updates).
    Przyjmuje pojedynczy obiekt albo liste."""
    raw = payload.get("updates")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return {"received": 0, "new": 0, "errors": ["brak pola updates"]}

    ts = int(time.time())
    new, errors = 0, []
    try:
        # surowiec w calosci (P3) - save_grade nizej wyciaga tylko wybrane
        # pola; awaria archiwum nie moze zablokowac zapisu oceny
        await asyncio.to_thread(db.save_grade_raw, raw, PLATFORM, ts)
    except Exception as e:
        errors.append(f"grade_raw: {type(e).__name__}: {e}")
    for entry in raw:
        try:
            if await asyncio.to_thread(db.save_grade, entry, PLATFORM, ts):
                new += 1
                await asyncio.to_thread(db.log_event, "grade", {
                    "grade": entry.get("grade"),
                    "champion_id": entry.get("championId"),
                    "game_id": entry.get("gameId"),
                }, ts)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    if new:
        # nowa obserwacja = trening od razu. Bez tego grade_model w settings
        # stoi na stanie sprzed oceny, a predykcje/readiness/targets czytaja
        # wlasnie jego. Trening na tej probce to ulamek sekundy; jego awaria
        # nie moze zablokowac zapisu oceny, stad oslona.
        try:
            await asyncio.to_thread(model.train, DEFAULT_MODE, True, GOAL)
        except Exception as e:
            await asyncio.to_thread(db.log_event, "model_train_fail",
                                    {"error": f"{type(e).__name__}: {e}"}, ts)
    return {"received": len(raw), "new": new, "errors": errors[:5]}


@write_api.post("/missions")
async def receive_missions(payload: dict):
    """(1) Misje maestrii z klienta - agent filtruje po slowach kluczowych
    i przysyla surowe obiekty; trzymamy ostatni stan."""
    missions = payload.get("missions") or []
    if not missions:
        return {"stored": False}
    await asyncio.to_thread(db.set_json_setting, "missions_state",
                            {"ts": int(time.time()), "missions": missions})
    return {"stored": True, "missions": len(missions)}


@api.get("/missions")
async def read_missions():
    return db.get_json_setting("missions_state") or {"missions": []}


@api.get("/recap")
async def recap(mode: str | None = None):
    """(16) Podsumowanie splitu - liczby z biezacego zakresu."""
    return await asyncio.to_thread(db.split_recap, mode or DEFAULT_MODE)


@write_api.post("/pass")
async def receive_pass(payload: dict):
    """Stan przepustki z event-hubu (karta 18) - agent przysyla po grze
    i przy starcie. Trzymamy ostatni stan w ustawieniach."""
    events = payload.get("events") or []
    if not events:
        return {"stored": False}
    await asyncio.to_thread(db.set_json_setting, "pass_state",
                            {"ts": int(time.time()), "events": events})
    return {"stored": True, "events": len(events)}


@api.get("/pass")
async def read_pass():
    """Stan przepustki + tempo grania + oczekiwane gry lidera rankingu
    (zapisywane przy kazdym /targets) - front sklada z tego zegar
    i przelacznik rezimu (3+19+32)."""
    st = db.get_json_setting("pass_state") or {}
    return {
        **st,
        "tempo": await asyncio.to_thread(db.recent_tempo, DEFAULT_MODE, 7),
        "best_expected": state.get("last_best_expected"),
        "projection": db.get_json_setting("mission_projection"),
    }


@write_api.post("/eventdata")
async def receive_eventdata(payload: dict):
    """Log zdarzen Live Client z konca gry - dane ulotne, przyjmujemy
    zawsze; link do meczu robi pozniejsza analiza po saved_at."""
    events = payload.get("events") or []
    if not events:
        return {"stored": False, "events": 0}
    await asyncio.to_thread(db.save_live_events, payload.get("champion_id"), events)
    return {"stored": True, "events": len(events)}


@write_api.post("/eog")
async def push_eog(payload: dict):
    """Agent wysyla tu caly blok ekranu koncowego z LCU."""
    block = payload.get("block")
    if not isinstance(block, dict):
        return {"stored": False, "errors": ["brak pola block"]}
    try:
        ts = int(time.time())
        new = await asyncio.to_thread(db.save_eog, block, PLATFORM, ts)

        gid = block.get("gameId") or block.get("gameID")
        match_id = f"{PLATFORM.upper()}_{gid}" if gid else None
        stats_rows = 0
        pool_id = None
        participants = 0
        if match_id:
            stats_rows = await asyncio.to_thread(db.flatten_eog_stats, block, match_id)
            participants = await asyncio.to_thread(
                db.save_match_participants, block, match_id)
            me = db._find_local_player(block)
            reroll = (block.get("rerollData") or {}).get("rerollCount")
            pool_id = await asyncio.to_thread(
                db.link_pool_to_match, match_id,
                db.normalize_champion_id(me.get("championId") or 0,
                                         state.get("last_queue_mode")),
                reroll, ts)

        await asyncio.to_thread(db.log_event, "eog", {
            "new": new, "stats_rows": stats_rows, "pool_id": pool_id,
            "participants": participants}, ts)
        return {"stored": True, "new": new, "stats_rows": stats_rows,
                "pool_id": pool_id, "participants": participants}
    except Exception as e:
        return {"stored": False, "errors": [f"{type(e).__name__}: {e}"]}


@api.get("/eog")
async def eog_summary():
    return db.eog_stats()


@api.get("/grades")
async def grades():
    return db.grade_stats()


@api.get("/grades/dataset")
async def grades_dataset(mode: str | None = None):
    return db.grades_with_stats(mode or DEFAULT_MODE)


@api.get("/splits")
async def splits():
    return {"current": db.current_split_id(), "splits": db.list_splits()}


@api.get("/events")
async def events(limit: int = 50, kind: str | None = None):
    return db.recent_events(limit, kind)


@api.get("/pools")
async def pools(limit: int = 50):
    return db.pool_history(limit)


@api.get("/stats/keys")
async def stats_keys():
    return db.stat_keys()


@api.get("/stats/share/{match_id}/{stat_key}")
async def stats_share(match_id: str, stat_key: str):
    r = db.my_share(match_id, stat_key)
    if r is None:
        raise HTTPException(404, "brak danych dla tego meczu lub pola")
    return r


@write_api.post("/grades/backfill")
async def grades_backfill(window: int = 7200):
    """Odzyskuje oceny z historii snapshotow. Bezpieczne do powtarzania."""
    return await asyncio.to_thread(db.backfill_grades_from_snapshots, window)


@write_api.post("/participants/backfill")
async def participants_backfill():
    """(karta 9) Tozsamosci graczy z zachowanych blobow eog_raw.
    Bezpieczne do powtarzania."""
    return await asyncio.to_thread(db.backfill_participants_from_eog)


@write_api.post("/balance/refresh")
async def balance_refresh():
    """(48) Reczny refresh mnoznikow balansu Mayhema, poza dobowa petla."""
    r = await state["plain"].get(balance.BALANCE_URL, follow_redirects=True)
    if r.status_code != 200:
        raise HTTPException(502, f"arammayhem: HTTP {r.status_code}")
    return await asyncio.to_thread(balance.store_balance, r.text)


@api.get("/balance")
async def get_balance():
    """(48) Ostatnio pobrane mnozniki balansu trybu, klucze = champion_id."""
    return db.get_json_setting("mayhem_balance") or {}


@write_api.post("/probe")
async def probe_create(payload: dict):
    """(42) Konsola LCU: zlecenie surowego GET-a do klienta gry. Wykonuje
    agent przy najblizszym obiegu (~3 s); tu tylko kolejka. Wylacznie
    odczyty - zadnych zapisow do LCU."""
    path = str(payload.get("path") or "").strip()
    if not path.startswith("/") or ".." in path or len(path) > 300:
        raise HTTPException(400, "sciezka musi byc absolutna w obrebie LCU")
    pid = await asyncio.to_thread(db.probe_create, path, int(time.time()))
    return {"id": pid}


@api.get("/probe/pending")
async def probe_pending():
    """Agent odpytuje to w petli - zwraca niewykonane sondy."""
    return {"probes": await asyncio.to_thread(db.probe_pending)}


@write_api.post("/probe/result")
async def probe_result(payload: dict):
    pid = payload.get("id")
    if not isinstance(pid, int):
        raise HTTPException(400, "brak id sondy")
    await asyncio.to_thread(db.probe_answer, pid, payload.get("http_status"),
                            str(payload.get("response") or ""), int(time.time()))
    return {"stored": True}


@api.get("/probe/{pid}")
async def probe_read(pid: int):
    r = await asyncio.to_thread(db.probe_get, pid)
    if not r:
        raise HTTPException(404, "nie ma takiej sondy")
    return r


@api.get("/history/missing")
async def history_missing(limit: int = 10):
    """(P6) Znane gry bez statystyk w match_player - agent dociaga je
    pojedynczo po ID przy bezczynnym kliencie."""
    return {"game_ids": await asyncio.to_thread(db.missing_own_games, limit)}


@write_api.post("/backup/report")
async def backup_report(payload: dict):
    """(P5) backup-server na Macu melduje tu wynik backupu. Dashboard
    zamiast ciszy - powiadomien nie ma z decyzji ostatecznej, ale porazka
    backupu nie moze zyc tylko w stdout launchd."""
    ok = bool(payload.get("ok"))
    note = str(payload.get("note") or "")[:500]
    ts = int(time.time())
    await asyncio.to_thread(db.set_json_setting, "last_backup",
                            {"ts": ts, "ok": ok, "note": note})
    await asyncio.to_thread(db.log_event, "backup_report",
                            {"ok": ok, "note": note[:120]}, ts)
    return {"stored": True}


@api.get("/model/status")
async def model_status(min_games: int = 40):
    return db.model_status(min_games)


@write_api.post("/model/train")
async def model_train(mode: str | None = None):
    return await asyncio.to_thread(model.train, mode or DEFAULT_MODE, True, GOAL)


@api.get("/model")
async def model_get():
    m = db.get_json_setting("grade_model")
    if not m:
        raise HTTPException(404, "model nie byl jeszcze trenowany")
    return m


@api.get("/model/rates")
async def model_rates(mode: str | None = None):
    return await asyncio.to_thread(model.champion_rates, mode or DEFAULT_MODE)


@api.get("/model/explain")
async def model_explain(mode: str | None = None):
    """Ostatnie mecze z predykcja obok faktycznej oceny - do sprawdzenia,
    czy model w ogole trafia."""
    rows = model.training_rows(mode or DEFAULT_MODE)
    out = []
    for r in rows[-25:]:
        out.append({
            "grade": r["grade"],
            "champion_id": r["champion_id"],
            "p_A": (model.predict(r, "A-") or {}).get("p"),
            "p_S": (model.predict(r, "S-") or {}).get("p"),
        })
    return out


@api.get("/grades/explain")
async def grades_explain(match_id: str):
    """Karta 13+27 - rozbicie oceny na sklad i percentyl."""
    out = await asyncio.to_thread(model.explain, match_id)
    if not out:
        raise HTTPException(404, "brak oceny lub statystyk dla tego meczu")
    return out


@api.get("/grades/history")
async def grades_history(limit: int = 60, mode: str | None = None):
    """Oceny z predykcja modelu obok tego, co faktycznie wypadlo.
    Kolejnosc po czasie obserwacji - bez ORDER BY SQLite zwraca dowolna."""
    use_mode = mode or DEFAULT_MODE
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT g.grade, g.champion_id, g.observed_at, g.match_id,
                   m.kills, m.deaths, m.assists, m.dmg_champ, m.gold,
                   m.cs, m.vision, m.heal, m.duration
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            WHERE m.duration > 300 AND m.game_mode = ?
            ORDER BY g.observed_at DESC""", (use_mode,))]
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM champion")}
        keys = {r["id"]: r["key"] for r in c.execute("SELECT id, key FROM champion")}

    out = []
    for r in rows[:limit]:
        pa = model.predict(r, "A-") or {}
        ps = model.predict(r, "S-") or {}
        fv = features.match_features(r)
        out.append({
            "grade": r["grade"],
            "match_id": r["match_id"],
            "champion_id": r["champion_id"],
            "name": names.get(r["champion_id"], str(r["champion_id"])),
            "key": keys.get(r["champion_id"]),
            "observed_at": r["observed_at"],
            "kills": r["kills"], "deaths": r["deaths"], "assists": r["assists"],
            "dmg": r["dmg_champ"], "gold": r["gold"], "duration": r["duration"],
            "gpm": round(fv["gpm"]),
            "dpm": round(fv["dpm"]),
            "p_A": pa.get("p"), "p_S": ps.get("p"),
            "censored": r["grade"].startswith(">="),
        })
    return {"count": len(rows), "grades": out}


@api.get("/split/progress")
async def split_progress():
    sid = db.latest_snapshot_id()
    if sid is None:
        raise HTTPException(400, "brak snapshotow")
    rows = db.snapshot_rows(sid)
    dist = {}
    marks = 0
    for r in rows:
        dist[r["milestone"]] = dist.get(r["milestone"], 0) + 1
        marks += r["tokens"] or 0

    ladder = db.get_ladder()
    with db.connect() as c:
        events = [dict(r) for r in c.execute(
            "SELECT ts, kind, detail FROM event_log WHERE kind IN "
            "('grade','split_reset') ORDER BY ts DESC LIMIT 30")]
        first = c.execute("SELECT MIN(taken_at) t FROM snapshot").fetchone()["t"]

    return {
        "goal": GOAL,
        "distribution": dict(sorted(dist.items())),
        "champions": len(rows),
        "marks_total": marks,
        "at_goal": dist.get(GOAL, 0),
        "ladder": ladder,
        "split": db.list_splits()[:1],
        "tracking_since": first,
        "events": events,
    }


@api.get("/split/timeline")
async def split_timeline(split: int | None = None):
    """Przebieg splitu w czasie z zapisanych snapshotow: suma marks
    i liczba championow na kazdym szczeblu. 62 snapshoty lezaly nieuzyte."""
    def build():
        with db.connect() as c:
            sid = split or (c.execute(
                "SELECT MAX(split_id) s FROM snapshot").fetchone()["s"] or 1)
            rows = [dict(r) for r in c.execute("""
                SELECT s.id, s.taken_at,
                       SUM(m.tokens) marks,
                       SUM(CASE WHEN m.milestone >= 1 THEN 1 ELSE 0 END) ms1,
                       SUM(CASE WHEN m.milestone >= 2 THEN 1 ELSE 0 END) ms2,
                       SUM(CASE WHEN m.milestone >= 3 THEN 1 ELSE 0 END) ms3,
                       SUM(CASE WHEN m.milestone >= 4 THEN 1 ELSE 0 END) ms4
                FROM snapshot s JOIN mastery m ON m.snapshot_id = s.id
                WHERE s.split_id = ?
                GROUP BY s.id ORDER BY s.taken_at""", (sid,))]
        return {"split_id": sid, "points": rows}
    return await asyncio.to_thread(build)


@api.get("/export", dependencies=[Depends(require_token)])
async def export_all():
    """Zrzut wszystkich tabel do JSON-a. Drugi kanal wyjscia obok restica -
    dane sa nieodtwarzalne, wiec jedna sciezka ratunku to za malo.
    Bloby (skompresowane eog) pomijamy: sa w backupie, a tu wazy najwiecej.
    Token jak na zapisach: pelny zrzut bazy to nie jest widok publiczny."""
    def dump():
        out = {}
        with db.connect() as con:
            tables = [r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")
                        if "payload" not in r["name"]]
                sel = ", ".join(cols)
                out[t] = [dict(r) for r in con.execute(f"SELECT {sel} FROM {t}")]
        return out
    data = await asyncio.to_thread(dump)
    from fastapi.responses import JSONResponse
    return JSONResponse(data, headers={
        "Content-Disposition": f"attachment; filename=mastery-export-{int(time.time())}.json"})


@api.get("/predictions/scorecard")
async def predictions_scorecard():
    """Uczciwosc modelu na predykcjach sprzed gry. Brier: 0 idealnie,
    0.25 to poziom rzucania moneta przy p=0.5."""
    from . import model as _m
    resolved, pending = await asyncio.to_thread(db.prediction_pairs)
    pairs = []
    for r in resolved:
        y = _m.label_for(r["grade"], r["threshold"])
        if y is None:
            continue
        pairs.append({"p": r["p"], "hit": y, "threshold": r["threshold"],
                      "grade": r["grade"], "ts": r["ts"]})
    out = {"resolved": len(pairs), "pending_pools": pending, "pairs": pairs[:50]}
    if pairs:
        out["brier"] = round(sum((x["p"] - x["hit"]) ** 2 for x in pairs) / len(pairs), 4)
        out["hit_rate"] = round(sum(x["hit"] for x in pairs) / len(pairs), 3)
        out["mean_p"] = round(sum(x["p"] for x in pairs) / len(pairs), 3)
    return out


@write_api.post("/snowball/candidates")
async def snowball_candidates(payload: dict):
    """Agent po kazdej grze przysyla puuid-y pozostalych graczy."""
    puuids = [p for p in payload.get("puuids", [])
              if isinstance(p, str) and len(p) == 36]
    ts = int(time.time())
    total = await asyncio.to_thread(db.snowball_add_candidates, puuids, ts)
    return {"received": len(puuids), "known_total": total}


@write_api.post("/snowball/ingest")
async def snowball_ingest(payload: dict):
    puuid = payload.get("puuid") or ""
    games = payload.get("games") or []
    kiwi, new_rows = await asyncio.to_thread(db.snowball_ingest, puuid, games)
    await asyncio.to_thread(db.snowball_mark, puuid, kiwi, new_rows)
    if new_rows:
        await asyncio.to_thread(db.log_event, "snowball_ingest",
                                {"puuid": puuid[:8], "kiwi": kiwi,
                                 "rows": new_rows}, int(time.time()))
    return {"kiwi": kiwi, "new_rows": new_rows}


@api.get("/snowball/next")
async def snowball_next(limit: int = 1):
    return {"puuids": await asyncio.to_thread(db.snowball_next, min(limit, 5))}


@api.get("/sentinel")
async def sentinel_status():
    return db.get_json_setting("mayhem_api") or {"open": False, "checked_at": 0}


@api.get("/limits")
async def limits():
    return db.LIMITER.status()


@api.get("/system/health")
async def system_health():
    with db.connect() as c:
        last = {r["kind"]: r["ts"] for r in c.execute(
            "SELECT kind, MAX(ts) ts FROM event_log GROUP BY kind")}
        counts = {}
        for t in ("match_player", "grade_observation", "eog_raw",
                  "champ_select_pool", "player_stat", "snapshot"):
            counts[t] = c.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        events = [dict(r) for r in c.execute(
            "SELECT ts, kind, detail FROM event_log ORDER BY ts DESC LIMIT 40")]
    return {
        "now": int(time.time()),
        "last_seen": last,
        "counts": counts,
        "model": db.model_status(),
        "ddragon_patch": db.get_setting("ddragon_patch"),
        "events": events,
        "gates": await asyncio.to_thread(db.data_gates),
        "pipeline": await asyncio.to_thread(db.pipeline_sanity),
        "last_backup": db.get_json_setting("last_backup"),
    }


@api.get("/lab/distribution")
async def lab_distribution(stat: str = "gpm", mode: str | None = None):
    """Rozklad wybranej statystyki w podziale na oceny."""
    rows = await asyncio.to_thread(model.training_rows, mode or DEFAULT_MODE)
    buckets = {}
    for r in rows:
        fv = features.match_features(r)
        val = {**fv,
               "kda": (r["kills"] + r["assists"]) / max(r["deaths"], 1),
               }.get(stat)
        if val is None:
            raise HTTPException(400, f"nieznana statystyka: {stat}")
        buckets.setdefault(r["grade"], []).append(round(val, 1))
    return {"stat": stat,
            "buckets": {k: sorted(v) for k, v in buckets.items()}}


@api.get("/model/readiness")
async def model_readiness():
    return await asyncio.to_thread(model.readiness)


@api.get("/model/targets/{champion_id}")
async def model_targets(champion_id: int, threshold: str = "S-",
                        mode: str | None = None):
    r = await asyncio.to_thread(model.targets_for, champion_id, threshold,
                                mode or DEFAULT_MODE)
    if r is None:
        raise HTTPException(404, "brak modelu albo danych dla tego championa")
    return r


@write_api.post("/live")
async def push_live(payload: dict):
    """Agent wysyla tu stan z Live Client Data (port 2999)."""
    if payload.get("ended"):
        await asyncio.to_thread(db.clear_live)
        return {"cleared": True}

    row = {
        "champion_id": payload.get("champion_id"),
        "champion": payload.get("champion"),
        "game_mode": payload.get("game_mode"),
        "game_time": payload.get("game_time"),
        "kills": payload.get("kills"), "deaths": payload.get("deaths"),
        "assists": payload.get("assists"), "cs": payload.get("cs"),
        "ward_score": payload.get("ward_score"),
        "gold_est": payload.get("gold_est"), "level": payload.get("level"),
        "payload": json.dumps(payload.get("raw") or {}),
        "updated_at": int(time.time()),
    }
    await asyncio.to_thread(db.set_live, row)
    return {"ok": True}


@api.get("/live")
async def read_live():
    live = await asyncio.to_thread(db.get_live)
    if not live:
        return {"active": False}

    fv = features.match_features(
        {"duration": live["game_time"], "gold": live["gold_est"],
         "kills": live["kills"], "assists": live["assists"],
         "deaths": live["deaths"], "cs": live["cs"]},
        floor=features.FLOOR_LIVE)
    mins = fv["minutes"]
    now = {
        "ka_per_min": round(fv["ka_per_min"], 2),
        "cs_per_min": round(fv["cs_per_min"], 2),
        "deaths_per_min": round(fv["deaths_per_min"], 2),
        "gold_per_min": round(fv["gpm"], 2),
    }

    # jaki prog obowiazuje na tym championie
    sid = db.latest_snapshot_id()
    need, milestone = None, None
    if sid and live["champion_id"]:
        for r in db.snapshot_rows(sid):
            if r["champion_id"] == live["champion_id"]:
                milestone = r["milestone"]
                nxt = json.loads(r["next_grades"] or "null")
                need = scoring.cheapest_grade(nxt)
                break

    ref = await asyncio.to_thread(
        db.reference_pace, need or "A-", live["game_mode"] or DEFAULT_MODE,
        live["champion_id"])

    # klucz DD do ikony - ten sam mechanizm co w /targets i historii ocen
    key = None
    if live["champion_id"]:
        with db.connect() as c:
            r = c.execute("SELECT key FROM champion WHERE id = ?",
                          (live["champion_id"],)).fetchone()
            key = r["key"] if r else None

    return {
        "active": True, "age": live["age"],
        "champion": live["champion"], "champion_id": live["champion_id"],
        "key": key,
        "milestone": milestone, "need": need,
        "game_time": live["game_time"], "minutes": round(mins, 1),
        "kills": live["kills"], "deaths": live["deaths"], "assists": live["assists"],
        "level": live["level"],
        "now": now, "reference": ref,
        "missing": ["obrażenia — Live Client Data ich nie udostępnia"],
    }


@api.get("/norms")
async def norms(stat: str = "totalDamageDealtToChampions", mode: str | None = None):
    """Rozklad danej statystyki per champion, zebrany ze wszystkich graczy
    w Mayhemie. Zastepuje zrodlo zewnetrzne, ktore tego trybu nie ma."""
    d = await asyncio.to_thread(db.champion_norms, stat, mode or DEFAULT_MODE)
    names = {}
    with db.connect() as c:
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM champion")}
    d["champions"] = {
        str(cid): {**v, "name": names.get(cid, str(cid))}
        for cid, v in sorted(d["champions"].items(), key=lambda kv: -kv[1]["mean"])}
    d["available_stats"] = db.NORM_STATS
    return d


app.include_router(api)
app.include_router(write_api)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
