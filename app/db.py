import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/code/data/mastery.db"))

GRADES = ["D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+",
          "A-", "A", "A+", "S-", "S", "S+"]
GRADE_RANK = {g: i for i, g in enumerate(GRADES)}

# Zrodla danych o meczu, od najbogatszego. LCU nie ma killParticipation
# ani tarcz, wiec nie moze nadpisac wiersza pochodzacego z API.
SOURCE_RANK = {"api": 2, "lcu": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS puuid_cache (
    riot_id    TEXT PRIMARY KEY,
    puuid      TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS champion (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestone_ladder (
    from_milestone INTEGER PRIMARY KEY,
    require_grades TEXT NOT NULL,
    games          INTEGER NOT NULL,
    reward_marks   INTEGER NOT NULL,
    bonus          INTEGER NOT NULL,
    observed_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mastery (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    champion_id     INTEGER NOT NULL,
    level           INTEGER NOT NULL,
    points          INTEGER NOT NULL,
    last_play       INTEGER,
    milestone       INTEGER NOT NULL,
    tokens          INTEGER NOT NULL,
    marks_next_lvl  INTEGER,
    next_grades     TEXT,
    next_games      INTEGER,
    next_marks      INTEGER,
    grades_earned   TEXT,
    PRIMARY KEY (snapshot_id, champion_id)
);

CREATE INDEX IF NOT EXISTS idx_mastery_champ ON mastery(champion_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_time ON snapshot(taken_at DESC);
"""

LOBBY_SCHEMA = """
DROP TABLE IF EXISTS lobby;
CREATE TABLE lobby (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    champion_ids TEXT NOT NULL,
    queue        TEXT,
    pool_kind    TEXT,
    updated_at   INTEGER NOT NULL
);
"""

MATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_ids (
    match_id      TEXT PRIMARY KEY,
    discovered_at INTEGER NOT NULL,
    fetched       INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS match_player (
    match_id      TEXT PRIMARY KEY,
    queue_id      INTEGER,
    game_mode     TEXT,
    game_creation INTEGER,
    duration      INTEGER,
    champion_id   INTEGER NOT NULL,
    win           INTEGER,
    kills         INTEGER, deaths INTEGER, assists INTEGER,
    kill_part     REAL,
    dmg_champ     INTEGER, dmg_obj INTEGER, dmg_taken INTEGER,
    heal          INTEGER, shield INTEGER,
    cs            INTEGER, vision INTEGER, gold INTEGER,
    position      TEXT
);

CREATE INDEX IF NOT EXISTS idx_mp_champ ON match_player(champion_id);
CREATE INDEX IF NOT EXISTS idx_mp_mode  ON match_player(game_mode);
CREATE INDEX IF NOT EXISTS idx_mi_todo  ON match_ids(fetched, failed);
"""


# ---------- polaczenie ----------

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    # WAL pozwala czytac w trakcie zapisu - bez tego sync w tle blokuje endpointy
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)


def init_lobby():
    with connect() as con:
        con.executescript(LOBBY_SCHEMA)


def init_matches():
    with connect() as con:
        con.executescript(MATCH_SCHEMA)


def upgrade_match_player():
    """Dodaje kolumny dokladane po pierwszym wydaniu schematu."""
    extra = {
        "source": "TEXT DEFAULT 'api'",
        "champion_id_raw": "INTEGER",
    }
    with connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(match_player)")}
        for name, ddl in extra.items():
            if name not in cols:
                con.execute(f"ALTER TABLE match_player ADD COLUMN {name} {ddl}")


# ---------- puuid ----------

def get_cached_puuid(riot_id):
    with connect() as con:
        row = con.execute("SELECT puuid FROM puuid_cache WHERE riot_id=?",
                          (riot_id,)).fetchone()
    return row["puuid"] if row else None


def cache_puuid(riot_id, puuid, ts):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO puuid_cache (riot_id, puuid, fetched_at) "
            "VALUES (:riot_id, :puuid, :ts)",
            {"riot_id": riot_id, "puuid": puuid, "ts": ts})


# ---------- championi ----------

def save_champions(champs):
    with connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO champion (id, name, key) VALUES (?, ?, ?)",
            champs)


def champion_count():
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM champion").fetchone()["c"]


# ---------- drabinka milestone ----------

def learn_ladder(entries, ts):
    rows = {}
    for e in entries:
        nxt = e.get("nextSeasonMilestone") or {}
        if not nxt.get("requireGradeCounts"):
            continue
        rows[e["championSeasonMilestone"]] = {
            "from_milestone": e["championSeasonMilestone"],
            "require_grades": json.dumps(nxt["requireGradeCounts"], sort_keys=True),
            "games": nxt.get("totalGamesRequires", 1),
            "reward_marks": nxt.get("rewardMarks", 0),
            "bonus": int(bool(nxt.get("bonus"))),
            "observed_at": ts,
        }
    with connect() as con:
        con.executemany(
            "INSERT OR IGNORE INTO milestone_ladder "
            "(from_milestone, require_grades, games, reward_marks, bonus, observed_at) "
            "VALUES (:from_milestone, :require_grades, :games, :reward_marks, :bonus, :observed_at)",
            list(rows.values()))


def get_ladder():
    with connect() as con:
        return {
            r["from_milestone"]: {
                "require_grades": json.loads(r["require_grades"]),
                "games": r["games"],
                "reward_marks": r["reward_marks"],
                "bonus": bool(r["bonus"]),
            }
            for r in con.execute("SELECT * FROM milestone_ladder")
        }


# ---------- snapshoty ----------

def save_snapshot(ts, entries):
    rows = []
    for e in entries:
        nxt = e.get("nextSeasonMilestone") or {}
        rows.append({
            "champion_id": e["championId"],
            "level": e["championLevel"],
            "points": e["championPoints"],
            "last_play": e.get("lastPlayTime"),
            "milestone": e["championSeasonMilestone"],
            "tokens": e.get("tokensEarned", 0),
            "marks_next_lvl": e.get("markRequiredForNextLevel"),
            "next_grades": json.dumps(nxt.get("requireGradeCounts")),
            "next_games": nxt.get("totalGamesRequires"),
            "next_marks": nxt.get("rewardMarks"),
            "grades_earned": json.dumps(e.get("milestoneGrades") or []),
        })
    with connect() as con:
        sid = con.execute("INSERT INTO snapshot (taken_at) VALUES (?)", (ts,)).lastrowid
        for r in rows:
            r["snapshot_id"] = sid
        con.executemany("""
            INSERT INTO mastery
              (snapshot_id, champion_id, level, points, last_play, milestone,
               tokens, marks_next_lvl, next_grades, next_games, next_marks, grades_earned)
            VALUES
              (:snapshot_id, :champion_id, :level, :points, :last_play, :milestone,
               :tokens, :marks_next_lvl, :next_grades, :next_games, :next_marks, :grades_earned)
        """, rows)
    return sid


def list_snapshots():
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT s.id, s.taken_at, COUNT(m.champion_id) champions "
            "FROM snapshot s LEFT JOIN mastery m ON m.snapshot_id=s.id "
            "GROUP BY s.id ORDER BY s.taken_at DESC")]


def latest_snapshot_id():
    with connect() as con:
        row = con.execute(
            "SELECT id FROM snapshot ORDER BY taken_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def snapshot_rows(sid):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT m.*, c.name, c.key FROM mastery m "
            "LEFT JOIN champion c ON c.id = m.champion_id "
            "WHERE m.snapshot_id=?", (sid,))]


def diff(from_id, to_id):
    with connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT b.champion_id, c.name,
                   a.points AS points_before, b.points AS points_after,
                   b.points - COALESCE(a.points,0) AS gained,
                   a.milestone AS ms_before, b.milestone AS ms_after,
                   a.tokens AS tokens_before, b.tokens AS tokens_after
            FROM mastery b
            LEFT JOIN mastery a ON a.champion_id=b.champion_id AND a.snapshot_id=?
            LEFT JOIN champion c ON c.id=b.champion_id
            WHERE b.snapshot_id=?
              AND (b.points - COALESCE(a.points,0) > 0
                   OR b.milestone != COALESCE(a.milestone,-1))
            ORDER BY gained DESC
        """, (from_id, to_id))]


# ---------- lobby ----------

def set_lobby(champion_ids, queue, pool_kind, ts):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO lobby (id, champion_ids, queue, pool_kind, updated_at) "
            "VALUES (1, :ids, :queue, :pool_kind, :ts)",
            {"ids": json.dumps(champion_ids), "queue": queue,
             "pool_kind": pool_kind, "ts": ts})


def get_lobby():
    with connect() as con:
        row = con.execute("SELECT * FROM lobby WHERE id=1").fetchone()
    if not row:
        return None
    return {
        "champion_ids": json.loads(row["champion_ids"]),
        "queue": row["queue"],
        "pool_kind": row["pool_kind"],
        "updated_at": row["updated_at"],
    }


# ---------- mecze ----------

MATCH_COLS = [
    "match_id", "queue_id", "game_mode", "game_creation", "duration",
    "champion_id", "win", "kills", "deaths", "assists", "kill_part",
    "dmg_champ", "dmg_obj", "dmg_taken", "heal", "shield",
    "cs", "vision", "gold", "position", "source", "champion_id_raw",
]

_MATCH_INSERT = (
    "INSERT INTO match_player (" + ", ".join(MATCH_COLS) + ") VALUES ("
    + ", ".join(f":{c}" for c in MATCH_COLS) + ")"
)


def _write_match(row):
    """Zapisuje mecz, o ile nowe zrodlo nie jest ubozsze od zapisanego.
    Zwraca True jesli wiersz byl nowy."""
    with connect() as con:
        cur = con.execute(
            "SELECT source FROM match_player WHERE match_id=?", (row["match_id"],)
        ).fetchone()

        if cur is not None:
            old_rank = SOURCE_RANK.get(cur["source"] or "api", 0)
            new_rank = SOURCE_RANK.get(row["source"], 0)
            if new_rank < old_rank:
                return False          # LCU nie nadpisuje danych z API
            con.execute("DELETE FROM match_player WHERE match_id=?", (row["match_id"],))

        con.execute(_MATCH_INSERT, row)
        con.execute(
            "INSERT OR IGNORE INTO match_ids (match_id, discovered_at, fetched) "
            "VALUES (:mid, :ts, 1)",
            {"mid": row["match_id"], "ts": (row["game_creation"] or 0) // 1000})
        con.execute("UPDATE match_ids SET fetched=1 WHERE match_id=?", (row["match_id"],))
    return cur is None


def add_match_ids(ids, ts):
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM match_ids").fetchone()["c"]
        con.executemany(
            "INSERT OR IGNORE INTO match_ids (match_id, discovered_at) VALUES (?,?)",
            [(i, ts) for i in ids])
        after = con.execute("SELECT COUNT(*) c FROM match_ids").fetchone()["c"]
    return after - before


def pending_match_ids(limit=None):
    q = "SELECT match_id FROM match_ids WHERE fetched=0 AND failed<3 ORDER BY match_id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with connect() as con:
        return [r["match_id"] for r in con.execute(q)]


def mark_failed(mid):
    with connect() as con:
        con.execute("UPDATE match_ids SET failed=failed+1 WHERE match_id=?", (mid,))


def save_match(mid, info, puuid):
    """Mecz z match-v5 (format v5). Zwraca True jesli wiersz byl nowy."""
    me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
    if me is None:
        with connect() as con:
            con.execute("UPDATE match_ids SET fetched=1 WHERE match_id=?", (mid,))
        return False

    ch = me.get("challenges") or {}
    row = {
        "match_id": mid,
        "queue_id": info.get("queueId"),
        "game_mode": info.get("gameMode"),
        "game_creation": info.get("gameCreation"),
        "duration": info.get("gameDuration"),
        "champion_id": me["championId"],
        "win": 1 if me.get("win") else 0,
        "kills": me.get("kills"),
        "deaths": me.get("deaths"),
        "assists": me.get("assists"),
        "kill_part": ch.get("killParticipation"),
        "dmg_champ": me.get("totalDamageDealtToChampions"),
        "dmg_obj": me.get("damageDealtToObjectives"),
        "dmg_taken": me.get("totalDamageTaken"),
        "heal": (me.get("totalHealsOnTeammates") or 0) + (me.get("totalHeal") or 0),
        "shield": me.get("totalDamageShieldedOnTeammates", 0),
        "cs": (me.get("totalMinionsKilled") or 0) + (me.get("neutralMinionsKilled") or 0),
        "vision": me.get("visionScore"),
        "gold": me.get("goldEarned"),
        "position": me.get("teamPosition") or None,
        "source": "api",
        "champion_id_raw": me["championId"],
    }
    return _write_match(row)


def normalize_champion_id(cid):
    """Tryby typu JADE przesuwaja ID o wielokrotnosc 1000 (60029 -> 29)."""
    cid = int(cid)
    return cid % 1000 if cid >= 1000 else cid


def save_lcu_game(g):
    """Mecz z historii LCU (stary format v4). Zwraca True jesli wiersz byl nowy."""
    parts = g.get("participants") or []
    if not parts:
        return False
    p = parts[0]
    if len(parts) > 1:
        ident = {i["participantId"]: i for i in (g.get("participantIdentities") or [])}
        mine = [x for x in parts if ident.get(x["participantId"])]
        p = mine[0] if mine else parts[0]

    st = p.get("stats") or {}
    tl = p.get("timeline") or {}
    raw_cid = p.get("championId", 0)

    row = {
        "match_id": f"{g.get('platformId', 'EUW1')}_{g['gameId']}",
        "queue_id": g.get("queueId"),
        "game_mode": g.get("gameMode"),
        "game_creation": g.get("gameCreation"),
        "duration": g.get("gameDuration"),
        "champion_id": normalize_champion_id(raw_cid),
        "win": 1 if st.get("win") else 0,
        "kills": st.get("kills"),
        "deaths": st.get("deaths"),
        "assists": st.get("assists"),
        "kill_part": None,                       # brak w formacie LCU
        "dmg_champ": st.get("totalDamageDealtToChampions"),
        "dmg_obj": st.get("damageDealtToObjectives"),
        "dmg_taken": st.get("totalDamageTaken"),
        "heal": st.get("totalHeal"),
        "shield": 0,                             # brak w formacie LCU
        "cs": (st.get("totalMinionsKilled") or 0) + (st.get("neutralMinionsKilled") or 0),
        "vision": st.get("visionScore"),
        "gold": st.get("goldEarned"),
        "position": tl.get("role") or tl.get("lane") or None,
        "source": "lcu",
        "champion_id_raw": raw_cid,
    }
    return _write_match(row)


# ---------- statystyki ----------

def history_stats():
    with connect() as con:
        known = con.execute("SELECT COUNT(*) c FROM match_ids").fetchone()["c"]
        done = con.execute("SELECT COUNT(*) c FROM match_ids WHERE fetched=1").fetchone()["c"]
        stored = con.execute("SELECT COUNT(*) c FROM match_player").fetchone()["c"]
        modes = [dict(r) for r in con.execute(
            "SELECT game_mode, COUNT(*) games, SUM(win) wins FROM match_player "
            "GROUP BY game_mode ORDER BY games DESC")]
    return {"known_ids": known, "fetched": done, "stored": stored, "by_mode": modes}


def mode_breakdown():
    with connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT game_mode, queue_id, source, COUNT(*) games, SUM(win) wins,
                   MIN(game_creation) oldest, MAX(game_creation) newest
            FROM match_player GROUP BY game_mode, queue_id, source
            ORDER BY games DESC""")]


def champion_stats(mode=None):
    return champion_stats_ex(mode)


def champion_stats_ex(mode=None, exclude_modes=()):
    where, args = [], []
    if mode:
        where.append("game_mode = ?")
        args.append(mode)
    for m in exclude_modes:
        where.append("game_mode != ?")
        args.append(m)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as con:
        return {r["champion_id"]: dict(r) for r in con.execute(f"""
            SELECT champion_id,
                   COUNT(*) AS games,
                   SUM(win) AS wins,
                   AVG(CAST(win AS REAL)) AS winrate,
                   AVG((kills + assists) / MAX(deaths, 1.0)) AS kda,
                   AVG(dmg_champ) AS avg_dmg,
                   MAX(game_creation) AS last_game
            FROM match_player {clause}
            GROUP BY champion_id
        """, args)}


def mode_prior(mode=None, exclude_modes=()):
    """Sredni winrate - punkt odniesienia przy sciaganiu malych probek."""
    stats = champion_stats_ex(mode, exclude_modes)
    games = sum(v["games"] for v in stats.values())
    wins = sum(v["wins"] or 0 for v in stats.values())
    return (wins / games) if games else 0.5, games


def latest_game_creation():
    with connect() as con:
        row = con.execute("SELECT MAX(game_creation) m FROM match_player").fetchone()
    return row["m"] if row and row["m"] else None
