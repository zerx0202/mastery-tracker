import json
import os
import sqlite3
import time
from pathlib import Path

from . import features

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
CREATE TABLE IF NOT EXISTS lobby (
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
    # UWAGA: NIE wlaczac WAL. Baza lezy na bind moncie przez virtiofs (Colima),
    # a WAL wymaga pliku -shm mapowanego w pamieci, czego virtiofs nie obsluguje
    # poprawnie. Objawia sie to bledem "disk I/O error" z wnetrza kontenera,
    # przy bazie calkowicie zdrowej widzianej z hosta. Zapalnikiem jest backup,
    # ktory czyta ten sam plik z hosta.
    # Przy jednym uzytkowniku domyslny journal + busy_timeout wystarcza.
    con.execute("PRAGMA busy_timeout = 10000")
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
        split_id = current_split_id()
        for r in rows.values():
            r["split_id"] = split_id
        # progi moga sie zmienic miedzy splitami - nadpisujemy w obrebie splitu
        con.executemany(
            "INSERT OR REPLACE INTO milestone_ladder "
            "(from_milestone, require_grades, games, reward_marks, bonus, observed_at, split_id) "
            "VALUES (:from_milestone, :require_grades, :games, :reward_marks, :bonus, "
            ":observed_at, :split_id)",
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
    split_id = current_split_id()
    with connect() as con:
        sid = con.execute("INSERT INTO snapshot (taken_at, split_id) VALUES (?,?)",
                          (ts, split_id)).lastrowid
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
    "game_version", "patch",
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
        "game_version": info.get("gameVersion"),
        "patch": short_patch(info.get("gameVersion")),
    }
    return _write_match(row)


def short_patch(version):
    """16.16.804.9184 -> 16.16"""
    if not version:
        return None
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


# Tryby, w ktorych Riot przesuwa identyfikatory championow.
OFFSET_MODES = {"JADE"}


def _mode_of(match_id):
    """Tryb meczu z bazy. Oceny i bloki koncowe go nie zawieraja, a bez niego
    nie wiadomo, czy id championa jest przesuniete."""
    with connect() as con:
        r = con.execute("SELECT game_mode FROM match_player WHERE match_id=?",
                        (match_id,)).fetchone()
    return r["game_mode"] if r else None


def normalize_champion_id(cid, game_mode=None):
    """W trybie JADE (league classic na boty) Riot dodaje do id offset +60000.
    Modulo stosujemy TYLKO tam - w innych trybach wysokie id to po prostu
    nowy champion i dzielenie zepsuloby dane."""
    if cid is None:
        return None
    cid = int(cid)
    if game_mode in OFFSET_MODES and cid >= 1000:
        return cid % 1000
    return cid

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
        "champion_id": normalize_champion_id(raw_cid, g.get("gameMode")),
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
        "game_version": g.get("gameVersion"),
        "patch": short_patch(g.get("gameVersion")),
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


GRADE_SCHEMA = """
CREATE TABLE IF NOT EXISTS grade_observation (
    match_id        TEXT PRIMARY KEY,
    game_id         INTEGER NOT NULL,
    champion_id     INTEGER NOT NULL,
    grade           TEXT NOT NULL,
    score           REAL,
    points_gained   INTEGER,
    points_contrib  INTEGER,
    points_before   INTEGER,
    level_after     INTEGER,
    leveled_up      INTEGER,
    tokens_earned   INTEGER,
    token_after     INTEGER,
    observed_at     INTEGER NOT NULL,
    source          TEXT,
    censored        INTEGER DEFAULT 0,
    threshold       TEXT,
    confidence      REAL,
    split_id        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_grade_champ ON grade_observation(champion_id);
CREATE INDEX IF NOT EXISTS idx_grade_grade ON grade_observation(grade);
"""

GRADE_COLS = [
    "match_id", "game_id", "champion_id", "grade", "score",
    "points_gained", "points_contrib", "points_before", "level_after",
    "leveled_up", "tokens_earned", "token_after", "observed_at", "split_id",
]


def init_grades():
    with connect() as con:
        con.executescript(GRADE_SCHEMA)


def save_grade(entry, platform, ts):
    """Zapisuje ocene pomeczowa z /lol-end-of-game/v1/champion-mastery-updates.
    Zwraca True jesli wpis byl nowy."""
    gid = entry.get("gameId")
    grade = entry.get("grade")
    if not gid or not grade:
        return False

    row = {
        "match_id": f"{platform.upper()}_{gid}",
        "game_id": gid,
        "champion_id": normalize_champion_id(
            entry.get("championId", 0), _mode_of(f"{platform.upper()}_{gid}")),
        "grade": grade,
        "score": entry.get("score"),
        "points_gained": entry.get("pointsGained"),
        "points_contrib": entry.get("pointsGainedIndividualContribution"),
        "points_before": entry.get("pointsBeforeGame"),
        "level_after": entry.get("level"),
        "leveled_up": int(bool(entry.get("hasLeveledUp"))),
        "tokens_earned": entry.get("tokensEarned"),
        "token_after": int(bool(entry.get("tokenEarnedAfterGame"))),
        "observed_at": ts,
        "split_id": current_split_id(),
    }
    sql = ("INSERT OR REPLACE INTO grade_observation (" + ", ".join(GRADE_COLS) +
           ") VALUES (" + ", ".join(f":{c}" for c in GRADE_COLS) + ")")
    with connect() as con:
        existed = con.execute(
            "SELECT 1 FROM grade_observation WHERE match_id=?", (row["match_id"],)
        ).fetchone() is not None
        con.execute(sql, row)
    return not existed


def grade_stats():
    """Rozklad ocen i pokrycie meczami - podstawa pod model p_A / p_S."""
    with connect() as con:
        total = con.execute("SELECT COUNT(*) c FROM grade_observation").fetchone()["c"]
        by_grade = [dict(r) for r in con.execute(
            "SELECT grade, COUNT(*) n FROM grade_observation GROUP BY grade")]
        joined = con.execute("""
            SELECT COUNT(*) c FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id""").fetchone()["c"]
        by_mode = [dict(r) for r in con.execute("""
            SELECT m.game_mode, g.grade, COUNT(*) n
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            GROUP BY m.game_mode, g.grade
            ORDER BY m.game_mode, n DESC""")]
    return {"total": total, "with_match_data": joined,
            "by_grade": by_grade, "by_mode": by_mode}


def grades_with_stats(mode=None):
    """Oceny sklejone ze statystykami meczu - dane treningowe modelu."""
    clause = "AND m.game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        return [dict(r) for r in con.execute(f"""
            SELECT g.grade, g.score, g.champion_id, c.name,
                   m.game_mode, m.queue_id, m.win, m.kills, m.deaths, m.assists,
                   m.dmg_champ, m.dmg_obj, m.dmg_taken, m.heal, m.cs, m.vision,
                   m.gold, m.duration, m.game_creation
            FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id
            LEFT JOIN champion c ON c.id = g.champion_id
            WHERE 1=1 {clause}
            ORDER BY m.game_creation DESC""", args)]


EOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS eog_raw (
    match_id     TEXT PRIMARY KEY,
    game_id      INTEGER NOT NULL,
    champion_id  INTEGER,
    augments     TEXT,
    payload      BLOB NOT NULL,
    captured_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eog_champ ON eog_raw(champion_id);
"""


def init_eog():
    with connect() as con:
        con.executescript(EOG_SCHEMA)


def _find_local_player(block):
    """Ekran koncowy: localPlayer albo pierwszy gracz w druzynie."""
    if isinstance(block.get("localPlayer"), dict):
        return block["localPlayer"]
    for team in block.get("teams") or []:
        for p in team.get("players") or []:
            if p.get("isLocalPlayer") or p.get("selfIndex"):
                return p
    for team in block.get("teams") or []:
        players = team.get("players") or []
        if players:
            return players[0]
    return {}


def save_eog(block, platform, ts):
    """Zapisuje caly blok ekranu koncowego (skompresowany) + wyciagniete augmenty.
    Trzymamy surowiec, bo nie wiemy jeszcze, ktore z ~183 pol beda potrzebne."""
    import zlib

    gid = block.get("gameId") or block.get("gameID")
    me = _find_local_player(block)
    if not gid:
        gid = me.get("gameId")
    if not gid:
        return False

    st = me.get("stats") or {}
    augments = [st.get(f"playerAugment{i}") or st.get(f"PLAYER_AUGMENT_{i}") or 0
                for i in range(1, 7)]

    row = {
        "match_id": f"{platform.upper()}_{gid}",
        "game_id": gid,
        "champion_id": normalize_champion_id(
            me.get("championId") or 0, _mode_of(f"{platform.upper()}_{gid}")),
        "augments": json.dumps([a for a in augments if a]),
        "payload": zlib.compress(json.dumps(block, separators=(",", ":")).encode()),
        "captured_at": ts,
    }
    with connect() as con:
        existed = con.execute(
            "SELECT 1 FROM eog_raw WHERE match_id=?", (row["match_id"],)).fetchone() is not None
        con.execute(
            "INSERT OR REPLACE INTO eog_raw "
            "(match_id, game_id, champion_id, augments, payload, captured_at) "
            "VALUES (:match_id, :game_id, :champion_id, :augments, :payload, :captured_at)",
            row)
    return not existed


def load_eog(match_id):
    import zlib
    with connect() as con:
        row = con.execute("SELECT payload FROM eog_raw WHERE match_id=?",
                          (match_id,)).fetchone()
    if not row:
        return None
    return json.loads(zlib.decompress(row["payload"]))


def eog_stats():
    with connect() as con:
        total = con.execute("SELECT COUNT(*) c FROM eog_raw").fetchone()["c"]
        size = con.execute(
            "SELECT COALESCE(SUM(LENGTH(payload)), 0) s FROM eog_raw").fetchone()["s"]
        with_grade = con.execute("""
            SELECT COUNT(*) c FROM eog_raw e
            JOIN grade_observation g ON g.match_id = e.match_id""").fetchone()["c"]
    return {"games": total, "with_grade": with_grade,
            "compressed_bytes": size}


# ============================================================
#  Splity, ustawienia, log zdarzen
# ============================================================

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS split (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    detected_at  INTEGER NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_ts   ON event_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event_log(kind);
"""


def init_extra():
    with connect() as con:
        con.executescript(EXTRA_SCHEMA)
        # kolumny dokladane po pierwszym wydaniu
        cols = {r["name"] for r in con.execute("PRAGMA table_info(match_player)")}
        if "game_version" not in cols:
            con.execute("ALTER TABLE match_player ADD COLUMN game_version TEXT")
        if "patch" not in cols:
            con.execute("ALTER TABLE match_player ADD COLUMN patch TEXT")

        scols = {r["name"] for r in con.execute("PRAGMA table_info(snapshot)")}
        if "split_id" not in scols:
            con.execute("ALTER TABLE snapshot ADD COLUMN split_id INTEGER")

        lcols = {r["name"] for r in con.execute("PRAGMA table_info(milestone_ladder)")}
        if "split_id" not in lcols:
            # drabinka moze sie zmienic miedzy splitami - klucz musi to uwzgledniac
            con.execute("ALTER TABLE milestone_ladder ADD COLUMN split_id INTEGER DEFAULT 1")

        gcols = {r["name"] for r in con.execute("PRAGMA table_info(grade_observation)")}
        if "split_id" not in gcols:
            con.execute("ALTER TABLE grade_observation ADD COLUMN split_id INTEGER")


# ---------- ustawienia ----------

def get_setting(key, default=None):
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(value)))


def get_json_setting(key, default=None):
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_json_setting(key, value):
    set_setting(key, json.dumps(value))


# ---------- log zdarzen ----------

def log_event(kind, detail=None, ts=None):
    import time as _t
    with connect() as con:
        con.execute("INSERT INTO event_log (ts, kind, detail) VALUES (?,?,?)",
                    (ts or int(_t.time()), kind,
                     detail if isinstance(detail, str) else json.dumps(detail) if detail else None))


def recent_events(limit=50, kind=None):
    q = "SELECT * FROM event_log"
    args = []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    with connect() as con:
        return [dict(r) for r in con.execute(q, args)]


# ---------- splity ----------

def current_split_id():
    with connect() as con:
        row = con.execute("SELECT id FROM split ORDER BY started_at DESC LIMIT 1").fetchone()
        if row:
            return row["id"]
        # pierwszy split - zakotwiczamy na najstarszym snapshocie
        first = con.execute("SELECT MIN(taken_at) t FROM snapshot").fetchone()["t"]
        import time as _t
        ts = first or int(_t.time())
        sid = con.execute(
            "INSERT INTO split (started_at, detected_at, note) VALUES (?,?,?)",
            (ts, int(_t.time()), "split poczatkowy")).lastrowid
        con.execute("UPDATE snapshot SET split_id=? WHERE split_id IS NULL", (sid,))
    return sid


def detect_split_reset(prev_sid, new_sid, ts):
    """Reset splitu: milestone'y masowo spadaja, a punkty maestrii nie.
    Zwraca id nowego splitu albo None."""
    if prev_sid is None:
        return None
    with connect() as con:
        row = con.execute("""
            SELECT
              SUM(CASE WHEN b.milestone < a.milestone THEN 1 ELSE 0 END) AS dropped,
              SUM(CASE WHEN a.milestone > 0 THEN 1 ELSE 0 END)           AS had,
              SUM(CASE WHEN b.points < a.points THEN 1 ELSE 0 END)       AS points_lost
            FROM mastery a
            JOIN mastery b ON b.champion_id = a.champion_id AND b.snapshot_id = ?
            WHERE a.snapshot_id = ?
        """, (new_sid, prev_sid)).fetchone()

    dropped = row["dropped"] or 0
    had = row["had"] or 0
    points_lost = row["points_lost"] or 0

    # reset = wiekszosc championow z milestone > 0 spadla, a punkty nie zniknely
    if had < 3 or dropped < max(3, int(0.7 * had)) or points_lost > 0:
        return None

    with connect() as con:
        sid = con.execute(
            "INSERT INTO split (started_at, detected_at, note) VALUES (?,?,?)",
            (ts, ts, f"reset wykryty: {dropped}/{had} championow cofnietych")).lastrowid
        con.execute("UPDATE snapshot SET split_id=? WHERE id=?", (sid, new_sid))
    log_event("split_reset", {"split_id": sid, "dropped": dropped, "had": had}, ts)
    return sid


def list_splits():
    with connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT s.*, COUNT(sn.id) snapshots,
                   MIN(sn.taken_at) first_snapshot, MAX(sn.taken_at) last_snapshot
            FROM split s LEFT JOIN snapshot sn ON sn.split_id = s.id
            GROUP BY s.id ORDER BY s.started_at DESC""")]


# ============================================================
#  Pula z champ selecta + wyplaszczone statystyki graczy
# ============================================================

POOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS champ_select_pool (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    queue         TEXT,
    queue_id      INTEGER,
    pool_kind     TEXT,
    champion_ids  TEXT NOT NULL,
    pool_size     INTEGER NOT NULL,
    picked_id     INTEGER,
    match_id      TEXT,
    reroll_count  INTEGER,
    split_id      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pool_ts    ON champ_select_pool(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pool_match ON champ_select_pool(match_id);

CREATE TABLE IF NOT EXISTS player_stat (
    match_id       TEXT NOT NULL,
    participant_no INTEGER NOT NULL,
    champion_id    INTEGER,
    team_id        INTEGER,
    is_local       INTEGER NOT NULL DEFAULT 0,
    stat_key       TEXT NOT NULL,
    stat_value     REAL,
    PRIMARY KEY (match_id, participant_no, stat_key)
);

CREATE INDEX IF NOT EXISTS idx_ps_key   ON player_stat(stat_key);
CREATE INDEX IF NOT EXISTS idx_ps_local ON player_stat(match_id, is_local);
CREATE INDEX IF NOT EXISTS idx_ps_champ ON player_stat(champion_id, stat_key);
"""


def init_pool():
    with connect() as con:
        con.executescript(POOL_SCHEMA)


def save_pool(champion_ids, queue, queue_id, pool_kind, ts):
    """Zapisuje pule z champ selecta. Nie duplikuje, jesli ta sama pula
    zostala juz zapisana i nie jest jeszcze przypisana do meczu."""
    if not champion_ids:
        return None
    ids_json = json.dumps(sorted(champion_ids))
    with connect() as con:
        last = con.execute(
            "SELECT id, champion_ids FROM champ_select_pool "
            "WHERE match_id IS NULL ORDER BY ts DESC LIMIT 1").fetchone()
        if last and last["champion_ids"] == ids_json:
            return last["id"]
        return con.execute("""
            INSERT INTO champ_select_pool
              (ts, queue, queue_id, pool_kind, champion_ids, pool_size, split_id)
            VALUES (:ts, :queue, :queue_id, :pool_kind, :ids, :size, :split)
        """, {"ts": ts, "queue": queue, "queue_id": queue_id, "pool_kind": pool_kind,
              "ids": ids_json, "size": len(champion_ids),
              "split": current_split_id()}).lastrowid


def link_pool_to_match(match_id, champion_id, reroll_count, ts, max_age=14400):
    """Po grze doklejamy do ostatniej niezamknietej puli: co wybrales
    i w ktorym meczu. Bez tego nie wiadomo, jaki mial byc wybor.

    UWAGA: reroll_count to relikt - od V25.13 ARAM nie ma rerolli, sa karty
    2-3 championow z systemem litosci. Nie budowac na tym polu logiki."""
    with connect() as con:
        row = con.execute(
            "SELECT id FROM champ_select_pool WHERE match_id IS NULL AND ts > ? "
            "ORDER BY ts DESC LIMIT 1", (ts - max_age,)).fetchone()
        if not row:
            return None
        con.execute(
            "UPDATE champ_select_pool SET picked_id=?, match_id=?, reroll_count=? WHERE id=?",
            (champion_id, match_id, reroll_count, row["id"]))
    return row["id"]


def pool_history(limit=100):
    with connect() as con:
        return [dict(r) for r in con.execute("""
            SELECT p.*, c.name AS picked_name
            FROM champ_select_pool p
            LEFT JOIN champion c ON c.id = p.picked_id
            ORDER BY p.ts DESC LIMIT ?""", (int(limit),))]


# ---------- wyplaszczanie statystyk ----------

def _is_camel(key):
    """Blok zwraca kazde pole dwa razy: TOTAL_DAMAGE i totalDamage.
    Zostawiamy jedna konwencje."""
    return not (key.isupper() or "_" in key and key.upper() == key)


def flatten_eog_stats(block, match_id):
    """Rozklada statystyki wszystkich 10 graczy na wiersze klucz-wartosc.
    Nie decydujemy z gory, ktore pola sa wazne - zapisujemy wszystkie."""
    rows = []
    n = 0
    for team in block.get("teams") or []:
        team_id = team.get("teamId")
        for p in team.get("players") or []:
            n += 1
            st = p.get("stats") or {}
            is_local = 1 if (p.get("isLocalPlayer") or p.get("selfIndex")) else 0
            for k, v in st.items():
                if not _is_camel(k):
                    continue
                if isinstance(v, bool):
                    v = int(v)
                if not isinstance(v, (int, float)):
                    continue
                rows.append({
                    "match_id": match_id,
                    "participant_no": n,
                    "champion_id": p.get("championId"),
                    "team_id": team_id,
                    "is_local": is_local,
                    "stat_key": k,
                    "stat_value": float(v),
                })

    # gdy blok nie oznacza gracza lokalnego, rozpoznajemy go po championId
    if rows and not any(r["is_local"] for r in rows):
        me = _find_local_player(block)
        mine = me.get("championId")
        for r in rows:
            if r["champion_id"] == mine:
                r["is_local"] = 1

    if not rows:
        return 0
    with connect() as con:
        con.execute("DELETE FROM player_stat WHERE match_id=?", (match_id,))
        con.executemany("""
            INSERT INTO player_stat
              (match_id, participant_no, champion_id, team_id, is_local, stat_key, stat_value)
            VALUES (:match_id, :participant_no, :champion_id, :team_id, :is_local,
                    :stat_key, :stat_value)""", rows)
    return len(rows)


def stat_keys():
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT stat_key, COUNT(*) n FROM player_stat GROUP BY stat_key ORDER BY stat_key")]


def my_share(match_id, stat_key):
    """Twoj udzial w stawce danej gry - normalizacja na dlugosc i tempo meczu."""
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT is_local, stat_value FROM player_stat "
            "WHERE match_id=? AND stat_key=?", (match_id, stat_key))]
    if not rows:
        return None
    mine = next((r["stat_value"] for r in rows if r["is_local"]), None)
    if mine is None:
        return None
    total = sum(r["stat_value"] for r in rows)
    better = sum(1 for r in rows if r["stat_value"] > mine)
    return {
        "value": mine,
        "share": (mine / total) if total else None,
        "rank_in_game": better + 1,
        "of": len(rows),
    }


def upgrade_grades():
    """Kolumny pod oceny odzyskane ze snapshotow."""
    with connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(grade_observation)")}
        for name, ddl in {
            "source": "TEXT DEFAULT 'lcu'",
            "censored": "INTEGER DEFAULT 0",       # 1 = znamy tylko prog, nie dokladna ocene
            "threshold": "TEXT",                    # prog przy obserwacji cenzurowanej
            "confidence": "TEXT DEFAULT 'exact'",   # exact | window | unmatched
        }.items():
            if name not in cols:
                con.execute(f"ALTER TABLE grade_observation ADD COLUMN {name} {ddl}")


def backfill_grades_from_snapshots(window=7200):
    """Odzyskuje oceny z historii snapshotow: przyrost tablicy grades_earned
    to nowa ocena, awans milestone'a to ocena >= progu (tablica sie zeruje).
    Doklejamy mecz po championie i bliskosci czasowej."""
    import time as _t

    ladder = get_ladder()

    def threshold_for(ms):
        step = ladder.get(ms) or {}
        req = step.get("require_grades") or {}
        if not req:
            return None
        return min(req.keys(), key=lambda g: GRADE_RANK.get(g, 99))

    with connect() as con:
        snaps = [dict(r) for r in con.execute(
            "SELECT id, taken_at FROM snapshot ORDER BY taken_at")]
        matches = [dict(r) for r in con.execute(
            "SELECT match_id, champion_id, game_creation, duration "
            "FROM match_player WHERE game_creation IS NOT NULL")]

    by_champ = {}
    for m in matches:
        by_champ.setdefault(m["champion_id"], []).append(m)

    def find_match(cid, ts):
        """Mecz tym championem zakonczony przed snapshotem, najblizszy w czasie."""
        best, best_gap = None, None
        for m in by_champ.get(cid, []):
            end = (m["game_creation"] or 0) / 1000 + (m["duration"] or 0)
            gap = ts - end
            if 0 <= gap <= window and (best_gap is None or gap < best_gap):
                best, best_gap = m, gap
        return best

    events = []
    prev = {}
    with connect() as con:
        for s in snaps:
            cur = {r["champion_id"]: (json.loads(r["grades_earned"] or "[]"), r["milestone"])
                   for r in con.execute(
                       "SELECT champion_id, grades_earned, milestone FROM mastery "
                       "WHERE snapshot_id=?", (s["id"],))}
            for cid, (grades, ms) in cur.items():
                old_g, old_ms = prev.get(cid, (None, None))
                if old_ms is None:
                    continue
                if len(grades) > len(old_g):
                    for g in grades[len(old_g):]:
                        events.append((s["taken_at"], cid, g, None, 0))
                elif ms > old_ms:
                    events.append((s["taken_at"], cid, None, threshold_for(old_ms), 1))
            prev = cur

    added = skipped = unmatched = 0
    now = int(_t.time())
    for ts, cid, grade, threshold, censored in events:
        m = find_match(cid, ts)
        if not m:
            unmatched += 1
            continue
        row = {
            "match_id": m["match_id"],
            "game_id": int(m["match_id"].split("_")[-1]),
            "champion_id": cid,
            "grade": grade or f">={threshold}" if (grade or threshold) else "?",
            "score": None, "points_gained": None, "points_contrib": None,
            "points_before": None, "level_after": None, "leveled_up": None,
            "tokens_earned": None, "token_after": None,
            "observed_at": ts, "split_id": current_split_id(),
            "source": "snapshot_diff", "censored": censored,
            "threshold": threshold, "confidence": "window",
        }
        cols = list(row.keys())
        with connect() as con:
            exists = con.execute("SELECT 1 FROM grade_observation WHERE match_id=?",
                                 (row["match_id"],)).fetchone()
            if exists:
                skipped += 1
                continue
            con.execute(
                "INSERT INTO grade_observation (" + ", ".join(cols) + ") VALUES ("
                + ", ".join(f":{c}" for c in cols) + ")", row)
            added += 1

    log_event("grade_backfill", {"added": added, "skipped": skipped,
                                 "unmatched": unmatched, "events": len(events)}, now)
    return {"events": len(events), "added": added,
            "skipped_existing": skipped, "unmatched": unmatched}


def model_status(min_games=40):
    """Marker: ile obserwacji mamy i czy juz warto stroic model."""
    with connect() as con:
        total = con.execute("SELECT COUNT(*) c FROM grade_observation").fetchone()["c"]
        joined = con.execute("""
            SELECT COUNT(*) c FROM grade_observation g
            JOIN player_stat p ON p.match_id = g.match_id AND p.is_local = 1
            """).fetchone()["c"]
        exact = con.execute(
            "SELECT COUNT(*) c FROM grade_observation WHERE COALESCE(censored,0)=0").fetchone()["c"]
        by_src = [dict(r) for r in con.execute(
            "SELECT COALESCE(source,'lcu') source, COUNT(*) n FROM grade_observation GROUP BY 1")]
    with connect() as con:
        with_match = con.execute("""
            SELECT COUNT(*) c FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id""").fetchone()["c"]
        with_rich = con.execute("""
            SELECT COUNT(DISTINCT g.match_id) c FROM grade_observation g
            JOIN player_stat p ON p.match_id = g.match_id AND p.is_local = 1""").fetchone()["c"]
    usable = with_match
    return {
        "grades_total": total,
        "grades_exact": exact,
        "grades_censored": total - exact,
        "grades_with_stats": joined,
        "grades_with_match_stats": with_match,
        "grades_with_full_stats": with_rich,
        "by_source": by_src,
        "threshold": min_games,
        "ready_for_tuning": usable >= min_games,
        "still_needed": max(0, min_games - usable),
    }


LIVE_SCHEMA = """
DROP TABLE IF EXISTS live_game;
CREATE TABLE live_game (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    champion_id  INTEGER,
    champion     TEXT,
    game_mode    TEXT,
    game_time    REAL,
    kills        INTEGER, deaths INTEGER, assists INTEGER,
    cs           INTEGER,
    ward_score   REAL,
    gold_est     INTEGER,
    level        INTEGER,
    payload      TEXT,
    updated_at   INTEGER NOT NULL
);
"""


def init_live():
    with connect() as con:
        con.executescript(LIVE_SCHEMA)


def set_live(row):
    with connect() as con:
        con.execute("""
            INSERT OR REPLACE INTO live_game
              (id, champion_id, champion, game_mode, game_time, kills, deaths,
               assists, cs, ward_score, gold_est, level, payload, updated_at)
            VALUES (1, :champion_id, :champion, :game_mode, :game_time, :kills,
                    :deaths, :assists, :cs, :ward_score, :gold_est, :level,
                    :payload, :updated_at)""", row)


def get_live(max_age=90):
    import time as _t
    with connect() as con:
        r = con.execute("SELECT * FROM live_game WHERE id=1").fetchone()
    if not r:
        return None
    d = dict(r)
    d["age"] = int(_t.time()) - d["updated_at"]
    return d if d["age"] <= max_age else None


def clear_live():
    with connect() as con:
        con.execute("DELETE FROM live_game WHERE id=1")


def reference_pace(threshold="A-", mode=None):
    """Tempo z gier, ktore skonczyly sie ocena >= progu. To jest punkt
    odniesienia dla overlaya: 'tak wygladaly Twoje udane gry'."""
    want = GRADE_RANK.get(threshold)
    clause = "AND m.game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        rows = [dict(r) for r in con.execute(f"""
            SELECT g.grade, m.kills, m.deaths, m.assists, m.cs, m.gold, m.duration
            FROM grade_observation g JOIN match_player m ON m.match_id = g.match_id
            WHERE m.duration > 300 {clause}""", args)]

    import statistics
    hit, miss = [], []
    for r in rows:
        g = r["grade"]
        rank = GRADE_RANK.get(g[2:].strip()) if g.startswith(">=") else GRADE_RANK.get(g)
        if rank is None:
            continue
        f = features.match_features(r)
        vals = {
            "ka_per_min": f["ka_per_min"],
            "cs_per_min": f["cs_per_min"],
            "deaths_per_min": f["deaths_per_min"],
            "gold_per_min": f["gpm"],
        }
        (hit if rank >= want else miss).append(vals)

    def med(rows_, key):
        v = [x[key] for x in rows_]
        return round(statistics.median(v), 2) if v else None

    keys = ["ka_per_min", "cs_per_min", "deaths_per_min", "gold_per_min"]
    return {
        "threshold": threshold,
        "hit_games": len(hit),
        "miss_games": len(miss),
        "hit": {k: med(hit, k) for k in keys},
        "miss": {k: med(miss, k) for k in keys},
    }


# ---------- limiter oparty na naglowkach Riota ----------

class RiotLimiter:
    """Riot podaje w naglowkach aktualne zuzycie limitow. Zamiast zgadywac
    odstepy, czytamy X-App-Rate-Limit-Count i czekamy tylko wtedy, gdy
    faktycznie zblizamy sie do sciany."""

    def __init__(self):
        self.retry_after = 0.0
        self.last_counts = {}

    def note(self, headers):
        import time as _t
        ra = headers.get("Retry-After")
        if ra:
            try:
                self.retry_after = _t.time() + float(ra)
            except ValueError:
                pass

        limit = headers.get("X-App-Rate-Limit") or ""
        count = headers.get("X-App-Rate-Limit-Count") or ""
        parsed = {}
        for lpart, cpart in zip(limit.split(","), count.split(","), strict=False):
            try:
                cap, window = lpart.split(":")
                used, w2 = cpart.split(":")
                if window == w2:
                    parsed[int(window)] = (int(used), int(cap))
            except ValueError:
                continue
        if parsed:
            self.last_counts = parsed

    def delay(self):
        """Ile sekund odczekac przed nastepnym zapytaniem."""
        import time as _t
        now = _t.time()
        if self.retry_after > now:
            return self.retry_after - now
        for window, (used, cap) in self.last_counts.items():
            if cap and used >= cap - 2:
                # zostaly ostatnie sloty w oknie - przeczekaj reszte okna
                return float(window) * 0.25
        return 0.0

    def status(self):
        return {"windows": {str(w): {"used": u, "cap": c}
                            for w, (u, c) in self.last_counts.items()},
                "cooldown": round(max(0.0, self.retry_after - __import__("time").time()), 1)}


LIMITER = RiotLimiter()


# ============================================================
#  Normalizator z danych Mayhema (wlasnych i cudzych)
# ============================================================
#
# Zewnetrzne serwisy nie maja Mayhema - aramstats.lol jawnie pisze, ze Riot
# nie wystawia tej kolejki w API. Zwykly ARAM nie jest przyblizeniem, bo
# augmenty nie skaluja wszystkich championow rowno, wiec ranking jest
# przetasowany. Za to eog-stats-block daje statystyki WSZYSTKICH dziesieciu
# graczy z kazdej gry - czyli 10 obserwacji o Mayhemie na mecz, w tym
# o championach, ktorymi sam nie gralem.

NORM_STATS = [
    "totalDamageDealtToChampions", "goldEarned", "totalMinionsKilled",
    "damageSelfMitigated", "totalDamageTaken", "totalHealsOnTeammates",
    "totalHeal", "timeCCingOthers", "totalDamageShieldedOnTeammates",
    "visionScore", "championLevel",
]

# Ponizej tylu obserwacji na championie ufamy bardziej sredniej globalnej.
NORM_SHRINK = 8.0


def champion_norms(stat_key="totalDamageDealtToChampions", mode=None, min_obs=1):
    """Srednia i odchylenie per champion, w przeliczeniu na minute,
    liczone ze wszystkich graczy w zebranych meczach.

    Wartosci per champion sa sciagane do sredniej globalnej proporcjonalnie
    do liczby obserwacji - przy dwoch grach na postaci wynik bedzie prawie
    rowny globalnemu i tak ma byc."""
    import statistics

    clause = "AND m.game_mode = ?" if mode else ""
    args = [stat_key] + ([mode] if mode else [])
    with connect() as con:
        rows = [dict(r) for r in con.execute(f"""
            SELECT p.champion_id, p.stat_value, m.duration
            FROM player_stat p
            JOIN norm_source m ON m.match_id = p.match_id
            WHERE p.stat_key = ? AND m.duration > 300 {clause}""", args)]

    per = {}
    allv = []
    for r in rows:
        v = r["stat_value"] / (r["duration"] / 60)
        per.setdefault(r["champion_id"], []).append(v)
        allv.append(v)

    if not allv:
        return {"stat": stat_key, "global": None, "champions": {}, "matches": 0}

    g_mean = statistics.mean(allv)
    g_sd = statistics.pstdev(allv) or 1.0

    out = {}
    for cid, vals in per.items():
        n = len(vals)
        if n < min_obs:
            continue
        m = statistics.mean(vals)
        sd = statistics.pstdev(vals) if n > 1 else g_sd
        w = n / (n + NORM_SHRINK)
        out[cid] = {
            "n": n,
            "mean_raw": round(m, 2),
            "mean": round(w * m + (1 - w) * g_mean, 2),
            "sd": round(max(w * sd + (1 - w) * g_sd, g_sd * 0.3), 2),
            "confidence": round(w, 2),
        }

    with connect() as con:
        nm = con.execute("SELECT COUNT(DISTINCT match_id) c FROM player_stat").fetchone()["c"]

    return {
        "stat": stat_key,
        "global": {"mean": round(g_mean, 2), "sd": round(g_sd, 2), "n": len(allv)},
        "champions": out,
        "matches": nm,
    }


_NORM_CACHE = {}
_NORM_TTL = 300  # sekund; po nowej grze normy maja sie przeliczyc, nie zamarzac


def norm_z(champion_id, stat_key, value_per_min, mode=None, cache=None):
    """Ile odchylen powyzej typowego wyniku na tym championie.
    To jest miara, ktora Riot faktycznie stosuje przy ocenie."""
    import time as _t
    store = _NORM_CACHE if cache is None else cache
    key = (stat_key, mode)
    hit = store.get(key)
    if hit is None or (cache is None and _t.time() - hit[0] > _NORM_TTL):
        hit = (_t.time(), champion_norms(stat_key, mode))
        store[key] = hit
    d = hit[1]
    if not d["global"]:
        return None
    c = d["champions"].get(champion_id)
    mean = c["mean"] if c else d["global"]["mean"]
    sd = c["sd"] if c else d["global"]["sd"]
    return {
        "z": round((value_per_min - mean) / (sd or 1.0), 3),
        "mean": mean, "sd": sd,
        "observations": (c or {}).get("n", 0),
        "confidence": (c or {}).get("confidence", 0.0),
    }


# ============================================================
#  Predykcje zapisywane PRZED gra
# ============================================================
#
# Walidacja LOO patrzy wstecz i modelowi latwo w niej wygladac dobrze.
# Predykcja zapisana w champ selekcie - zanim wynik istnieje - to jedyny
# test, ktorego nie da sie oszukac. Po grze link_pool_to_match przypina
# pule do meczu, ocena wpada do grade_observation i para
# "przewidywanie -> wynik" sklada sie sama.

PRED_SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_prediction (
    pool_id     INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,
    threshold   TEXT,
    p           REAL,
    specific    INTEGER DEFAULT 0,
    own_games   INTEGER,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (pool_id, champion_id)
);
"""


def save_pool_predictions(pool_id, rows, ts):
    with connect() as con:
        con.executescript(PRED_SCHEMA)
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO pool_prediction "
                "(pool_id, champion_id, threshold, p, specific, own_games, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pool_id, r["champion_id"], r.get("next_grade"), r.get("model_p"),
                 1 if r.get("model_specific") else 0, r.get("model_own_games"), ts))
    return len(rows)


def prediction_pairs():
    """Predykcje sprzed gry sparowane z tym, co faktycznie wypadlo.
    Tylko champion, ktorego naprawde wybrano - reszta puli nie ma wyniku."""
    with connect() as con:
        con.executescript(PRED_SCHEMA)
        resolved = [dict(r) for r in con.execute("""
            SELECT pp.p, pp.threshold, pp.specific, g.grade, csp.ts
            FROM pool_prediction pp
            JOIN champ_select_pool csp
              ON csp.id = pp.pool_id AND csp.picked_id = pp.champion_id
            JOIN grade_observation g ON g.match_id = csp.match_id
            WHERE pp.p IS NOT NULL
            ORDER BY csp.ts DESC""")]
        pending = con.execute("""
            SELECT COUNT(DISTINCT pp.pool_id) c
            FROM pool_prediction pp
            LEFT JOIN champ_select_pool csp
              ON csp.id = pp.pool_id AND csp.match_id IS NOT NULL
            WHERE csp.id IS NULL""").fetchone()["c"]
    return resolved, pending


def migrate():
    """Jedyny punkt wejscia do schematu. Odpala wszystkie init_* i upgrade_*
    w kolejnosci definicji w pliku - nowa funkcja migracyjna dopisana na koncu
    zostanie wykonana automatycznie, bez pamietania o lifespanie.

    Kazda funkcja musi byc idempotentna (CREATE IF NOT EXISTS / ALTER tylko
    gdy kolumny brak) - pilnuje tego test podwojnego uruchomienia."""
    import sys
    mod = sys.modules[__name__]
    fns = sorted(
        (getattr(mod, n) for n in dir(mod)
         if (n.startswith("init") or n.startswith("upgrade"))
         and callable(getattr(mod, n))),
        key=lambda f: f.__code__.co_firstlineno)
    for fn in fns:
        fn()
    with connect() as con:
        con.execute(f"PRAGMA user_version = {len(fns)}")
    return [f.__name__ for f in fns]


def init_predictions():
    """pool_prediction w jednym punkcie wejscia do schematu - dotad tabela
    powstawala leniwie przy pierwszym zapisie, jako jedyna poza migrate()."""
    with connect() as con:
        con.executescript(PRED_SCHEMA)


# ============================================================
#  Snowball: rejestr graczy z wlasnych meczow
# ============================================================
#
# Sonda potwierdzila: LCU oddaje historie obcych puuid. Kazdy wlasny mecz
# daje 9 kandydatow; ich gry KIWI to paliwo dla normalizatora (player_stat).
# Zabezpieczenia: gracz odpytywany najwyzej raz na REVISIT_DAYS, mecze
# deduplikowane po match_id - powtorka gracza wnosi tylko nowe gry.

SNOWBALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS snowball_seen (
    puuid      TEXT PRIMARY KEY,
    added_at   INTEGER NOT NULL,
    checked_at INTEGER DEFAULT 0,
    kiwi_games INTEGER DEFAULT 0,
    new_rows   INTEGER DEFAULT 0
);
"""

SNOWBALL_REVISIT_DAYS = 7


def init_snowball():
    with connect() as con:
        con.executescript(SNOWBALL_SCHEMA)


def snowball_add_candidates(puuids, ts):
    """Nowi kandydaci z wlasnych meczow. Znani sa ignorowani."""
    with connect() as con:
        for p in puuids:
            con.execute("INSERT OR IGNORE INTO snowball_seen (puuid, added_at) "
                        "VALUES (?, ?)", (p, ts))
        return con.execute("SELECT COUNT(*) c FROM snowball_seen").fetchone()["c"]


def snowball_next(limit=1):
    """Gracze do sprawdzenia: nigdy nie sprawdzani albo starsi niz okno."""
    cutoff = int(time.time()) - SNOWBALL_REVISIT_DAYS * 86400
    with connect() as con:
        return [r["puuid"] for r in con.execute(
            "SELECT puuid FROM snowball_seen WHERE checked_at < ? "
            "ORDER BY checked_at ASC, added_at ASC LIMIT ?", (cutoff, limit))]


def snowball_mark(puuid, kiwi_games, new_rows):
    with connect() as con:
        con.execute("UPDATE snowball_seen SET checked_at=?, kiwi_games=?, "
                    "new_rows=new_rows+? WHERE puuid=?",
                    (int(time.time()), kiwi_games, new_rows, puuid))


SNOWBALL_MATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS snowball_match (
    game_id   INTEGER PRIMARY KEY,
    duration  INTEGER NOT NULL,
    game_mode TEXT,
    queue_id  INTEGER,
    game_ts   INTEGER,
    from_puuid TEXT
);
CREATE VIEW IF NOT EXISTS norm_source AS
    SELECT match_id, duration, game_mode FROM match_player
    UNION ALL
    SELECT 'SB_' || game_id, duration, game_mode FROM snowball_match;
"""


def init_snowball_match():
    with connect() as con:
        con.executescript(SNOWBALL_MATCH_SCHEMA)


def snowball_ingest(puuid, games):
    """Gry KIWI obcego gracza z historii LCU -> snowball_match + player_stat.
    Historia LCU per gracz ma JEDNEGO uczestnika, wiec kazda gra to jedna
    obserwacja (champion, statystyki, czas). Dedup po game_id; gry, w ktorych
    sam gralem (pelne 10 obserwacji z wlasnego eog), sa pomijane."""
    kiwi = new_rows = 0
    with connect() as con:
        for g in games or []:
            gid = g.get("gameId")
            mode = g.get("gameMode")
            qid = g.get("queueId")
            if not gid or (qid != 2400 and mode != "KIWI"):
                continue
            kiwi += 1
            dur = int(g.get("gameDuration") or 0)
            if dur > 10000:          # niektore zrodla daja milisekundy
                dur //= 1000
            if dur <= 300:
                continue
            own = con.execute(
                "SELECT 1 FROM match_player WHERE match_id LIKE ?",
                (f"%{gid}",)).fetchone()
            if own:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO snowball_match "
                "(game_id, duration, game_mode, queue_id, game_ts, from_puuid) "
                "VALUES (?,?,?,?,?,?)",
                (gid, dur, mode, qid, int((g.get("gameCreation") or 0) / 1000),
                 puuid))
            if cur.rowcount == 0:    # juz znana z wczesniejszego snowballa
                continue
            part = (g.get("participants") or [{}])[0]
            cid = normalize_champion_id(part.get("championId"), mode)
            stats = part.get("stats") or {}
            mid = f"SB_{gid}"
            for k, v in stats.items():
                if isinstance(v, bool):
                    v = int(v)
                if not isinstance(v, (int, float)):
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO player_stat "
                    "(match_id, participant_no, champion_id, team_id, is_local, "
                    "stat_key, stat_value) VALUES (?,?,?,?,0,?,?)",
                    (mid, 1, cid, part.get("teamId") or 0, k, v))
                new_rows += 1
    return kiwi, new_rows
