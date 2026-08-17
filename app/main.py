import json
import os
import time
from contextlib import asynccontextmanager
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import db
from .db import GRADE_RANK

API_KEY = os.environ["RIOT_API_KEY"]
PLATFORM = os.environ.get("RIOT_PLATFORM", "euw1")
REGION = os.environ.get("RIOT_REGION", "europe")
MY_NAME = os.environ["MY_RIOT_NAME"]
MY_TAG = os.environ["MY_RIOT_TAG"]
GOAL = int(os.environ.get("GOAL_MILESTONE", "4"))

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    state["client"] = httpx.AsyncClient(
        headers={"X-Riot-Token": API_KEY}, timeout=15.0)
    state["plain"] = httpx.AsyncClient(timeout=15.0)
    yield
    await state["client"].aclose()
    await state["plain"].aclose()


def norm(x: str) -> str:
    """Luzne dopasowanie nazw: Dr. Mundo == drmundo == dr mundo."""
    return re.sub(r"[^a-z0-9]", "", x.lower())


app = FastAPI(title="Mastery Tracker", lifespan=lifespan)
api = APIRouter(prefix="/api")


async def riot_get(url):
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


@api.get("/health")
async def health():
    return {"status": "ok", "platform": PLATFORM, "champions_cached": db.champion_count()}


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
    return {
        "known": known,
        "missing": [m for m in range(GOAL) if m not in known],
        "goal": GOAL,
    }


def cheapest_grade(req):
    """Najlatwiejsza ocena spelniajaca wymog."""
    if not req:
        return None
    return min(req.keys(), key=lambda g: GRADE_RANK.get(g, 99))


def path_to_goal(current, ladder):
    """Ile gier i jakie oceny od current do GOAL. None jesli drabinka nieznana."""
    games, steps, marks = 0, [], 0
    for ms in range(current, GOAL):
        step = ladder.get(ms)
        if step is None:
            return None, steps, marks
        games += step["games"]
        marks += step["reward_marks"]
        steps.append({
            "from": ms, "to": ms + 1,
            "grade": cheapest_grade(step["require_grades"]),
            "games": step["games"],
        })
    return games, steps, marks


@api.get("/targets")
async def targets(limit: int = 30, only: str | None = None):
    """Championi posortowani po tym, jak blisko sa GOAL_MILESTONE."""
    sid = db.latest_snapshot_id()
    if sid is None:
        raise HTTPException(400, "Brak snapshotow - zrob POST /snapshot")

    ladder = db.get_ladder()
    wanted = None
    if only:
        wanted = {norm(s) for s in only.split(",") if s.strip()}

    out = []
    for r in db.snapshot_rows(sid):
        name = r["name"] or str(r["champion_id"])
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
            "path_complete": games is not None,
            "level": r["level"],
            "points": r["points"],
            "tokens": r["tokens"],
            "last_play": r["last_play"],
        })

    out.sort(key=lambda x: (
        x["games_to_goal"] if x["games_to_goal"] is not None else 99,
        -x["milestone"],
        GRADE_RANK.get(x["next_grade"], 99),
        -x["points"],
    ))
    return {"goal": GOAL, "snapshot_id": sid, "targets": out[:limit]}


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
    return {
        "from_id": from_id, "to_id": to_id,
        "total_gained": sum(r["gained"] or 0 for r in rows),
        "champions": rows,
    }


app.include_router(api)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
