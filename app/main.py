import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import db, scoring
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.init_lobby()
    db.init_matches()
    db.upgrade_match_player()
    db.init_grades()
    state["limiter"] = RateLimiter()
    state["sync"] = {"running": False, "done": 0, "total": 0, "msg": "nie uruchomiony"}
    state["weights"] = dict(scoring.DEFAULT_WEIGHTS)
    state["client"] = httpx.AsyncClient(headers={"X-Riot-Token": API_KEY}, timeout=15.0)
    state["plain"] = httpx.AsyncClient(timeout=15.0)
    yield
    await state["client"].aclose()
    await state["plain"].aclose()


app = FastAPI(title="Mastery Tracker", lifespan=lifespan)
api = APIRouter(prefix="/api")


async def riot_get(url):
    lim = state.get("limiter")
    if lim:
        await lim.acquire()
    r = await state["client"].get(url)
    if r.status_code == 429:
        raise HTTPException(429, f"Rate limit, ponow za {r.headers.get('Retry-After','?')}s")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


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


@api.post("/refresh-champions")
async def refresh_champions():
    """Data Dragon - nazwy i klucze ikon. Bez klucza API, bez limitow."""
    vers = (await state["plain"].get(
        "https://ddragon.leagueoflegends.com/api/versions.json")).json()
    patch = vers[0]
    data = (await state["plain"].get(
        f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json")).json()
    champs = [(int(v["key"]), v["name"], v["id"]) for v in data["data"].values()]
    db.save_champions(champs)
    return {"patch": patch, "champions": len(champs)}


@api.post("/snapshot")
async def snapshot():
    puuid = await my_puuid()
    data = await riot_get(
        f"https://{PLATFORM}.api.riotgames.com"
        f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}")
    ts = int(time.time())
    db.learn_ladder(data, ts)
    sid = db.save_snapshot(ts, data)
    return {"snapshot_id": sid, "taken_at": ts, "champions": len(data)}


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


@api.get("/targets")
async def targets(limit: int = 30, only: str | None = None,
                  ids: str | None = None, mode: str | None = None):
    sid = db.latest_snapshot_id()
    if sid is None:
        raise HTTPException(400, "Brak snapshotow - zrob POST /snapshot")

    ladder = db.get_ladder()

    wanted_ids = None
    if ids:
        wanted_ids = {int(x) for x in ids.split(",") if x.strip()}
    wanted = None
    if only:
        wanted = {norm(s) for s in only.split(",") if s.strip()}

    use_mode = mode or DEFAULT_MODE
    stats = db.champion_stats_ex(use_mode, () if use_mode else EXCLUDED_MODES)
    prior, prior_games = db.mode_prior(use_mode, () if use_mode else EXCLUDED_MODES)

    out = []
    for r in db.snapshot_rows(sid):
        name = r["name"] or str(r["champion_id"])
        if wanted_ids is not None and r["champion_id"] not in wanted_ids:
            continue
        if wanted and norm(name) not in wanted and norm(r["key"] or "") not in wanted:
            continue
        if r["milestone"] >= GOAL:
            continue

        games, steps, marks = path_to_goal(r["milestone"], ladder)
        next_req = json.loads(r["next_grades"] or "null")

        out.append({
            "champion_id": r["champion_id"],
            "name": name,
            "key": r["key"],
            "milestone": r["milestone"],
            "next_grade": cheapest_grade(next_req),
            "next_games": r["next_games"],
            "grades_earned": json.loads(r["grades_earned"] or "[]"),
            "games_to_goal": games,
            "games_known": sum(st["games"] for st in steps),
            "marks_known": marks,
            "path": steps,
            "level": r["level"],
            "points": r["points"],
            "tokens": r["tokens"],
            "last_play": r["last_play"],
        })

    scoring.score_rows(out, stats, prior, state.get("weights"), int(time.time()), GOAL)
    return {
        "goal": GOAL,
        "snapshot_id": sid,
        "mode": use_mode,
        "prior_winrate": round(prior, 3),
        "prior_games": prior_games,
        "targets": out[:limit],
    }


@api.get("/weights")
async def get_weights():
    return {"weights": state.get("weights"), "defaults": scoring.DEFAULT_WEIGHTS}


@api.post("/weights")
async def set_weights(payload: dict):
    w = dict(scoring.DEFAULT_WEIGHTS)
    for k, v in (payload or {}).items():
        if k in w:
            w[k] = float(v)
    state["weights"] = w
    return {"weights": w}


# ---------------- lobby ----------------

@api.post("/lobby")
async def push_lobby(payload: dict):
    ids = [int(x) for x in payload.get("champion_ids", [])]
    db.set_lobby(ids, payload.get("queue"), payload.get("pool_kind"), int(time.time()))
    state["last_queue_id"] = payload.get("queue_id")
    return {"ok": True, "count": len(ids), "pool_kind": payload.get("pool_kind")}


@api.get("/lobby")
async def read_lobby(max_age: int = 900):
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
        "prior_winrate": t["prior_winrate"],
        "prior_games": t["prior_games"],
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


@api.post("/history/sync")
async def history_sync(full: bool = False):
    sync = state["sync"]
    if sync["running"]:
        return {"already_running": True, **sync}
    sync.update({"running": True, "done": 0, "total": 0, "msg": "startuje", "full": full})
    asyncio.create_task(sync_worker())
    return {"started": True}


@api.post("/history/stop")
async def history_stop():
    state["sync"]["running"] = False
    return {"stopping": True}


@api.get("/history/status")
async def history_status():
    return {**state["sync"], **db.history_stats()}


@api.post("/history/lcu")
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
    return {"received": len(games), "new": new, "errors": errors[:5]}


@api.get("/history/modes")
async def history_modes():
    return db.mode_breakdown()


@api.post("/grade")
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
    for entry in raw:
        try:
            if await asyncio.to_thread(db.save_grade, entry, PLATFORM, ts):
                new += 1
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    return {"received": len(raw), "new": new, "errors": errors[:5]}


@api.get("/grades")
async def grades():
    return db.grade_stats()


@api.get("/grades/dataset")
async def grades_dataset(mode: str | None = None):
    return db.grades_with_stats(mode or DEFAULT_MODE)


app.include_router(api)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
