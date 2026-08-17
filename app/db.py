import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/code/data/mastery.db"))

GRADES = ["D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+",
          "A-", "A", "A+", "S-", "S", "S+"]
GRADE_RANK = {g: i for i, g in enumerate(GRADES)}

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


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)


def get_cached_puuid(riot_id):
    with connect() as con:
        row = con.execute("SELECT puuid FROM puuid_cache WHERE riot_id=?",
                          (riot_id,)).fetchone()
    return row["puuid"] if row else None


def cache_puuid(riot_id, puuid, ts):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO puuid_cache VALUES (?,?,?)",
                    (riot_id, puuid, ts))


def save_champions(champs):
    with connect() as con:
        con.executemany("INSERT OR REPLACE INTO champion VALUES (?,?,?)", champs)


def champion_count():
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM champion").fetchone()["c"]


def learn_ladder(entries, ts):
    """Zapisuje nowo zaobserwowane progi drabinki milestone'ow."""
    rows = {}
    for e in entries:
        nxt = e.get("nextSeasonMilestone") or {}
        if not nxt.get("requireGradeCounts"):
            continue
        rows[e["championSeasonMilestone"]] = (
            e["championSeasonMilestone"],
            json.dumps(nxt["requireGradeCounts"], sort_keys=True),
            nxt.get("totalGamesRequires", 1),
            nxt.get("rewardMarks", 0),
            int(bool(nxt.get("bonus"))),
            ts,
        )
    with connect() as con:
        con.executemany(
            "INSERT OR IGNORE INTO milestone_ladder VALUES (?,?,?,?,?,?)",
            list(rows.values()),
        )


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


def save_snapshot(ts, entries):
    with connect() as con:
        sid = con.execute("INSERT INTO snapshot (taken_at) VALUES (?)",
                          (ts,)).lastrowid
        con.executemany(
            "INSERT INTO mastery VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    sid,
                    e["championId"],
                    e["championLevel"],
                    e["championPoints"],
                    e.get("lastPlayTime"),
                    e["championSeasonMilestone"],
                    e.get("tokensEarned", 0),
                    e.get("markRequiredForNextLevel"),
                    json.dumps((e.get("nextSeasonMilestone") or {}).get("requireGradeCounts")),
                    (e.get("nextSeasonMilestone") or {}).get("totalGamesRequires"),
                    (e.get("nextSeasonMilestone") or {}).get("rewardMarks"),
                    json.dumps(e.get("milestoneGrades") or []),
                )
                for e in entries
            ],
        )
    return sid


def list_snapshots():
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT s.id, s.taken_at, COUNT(m.champion_id) champions "
            "FROM snapshot s LEFT JOIN mastery m ON m.snapshot_id=s.id "
            "GROUP BY s.id ORDER BY s.taken_at DESC")]


def latest_snapshot_id():
    with connect() as con:
        row = con.execute("SELECT id FROM snapshot ORDER BY taken_at DESC LIMIT 1").fetchone()
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
              AND (b.points - COALESCE(a.points,0) > 0 OR b.milestone != COALESCE(a.milestone,-1))
            ORDER BY gained DESC
        """, (from_id, to_id))]


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


def init_lobby():
    with connect() as con:
        con.executescript(LOBBY_SCHEMA)


def set_lobby(champion_ids, queue, pool_kind, ts):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO lobby VALUES (1,?,?,?,?)",
            (json.dumps(champion_ids), queue, pool_kind, ts),
        )


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
