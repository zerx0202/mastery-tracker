import json
import re
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
    from_milestone INTEGER NOT NULL,
    require_grades TEXT NOT NULL,
    games          INTEGER NOT NULL,
    reward_marks   INTEGER NOT NULL,
    bonus          INTEGER NOT NULL,
    observed_at    INTEGER NOT NULL,
    split_id       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (from_milestone, split_id)
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
    updated_at   INTEGER NOT NULL,
    trade_ids    TEXT
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

def upgrade_champion_tags():
    """Klasy z Data Dragona (Tank, Mage, Support...) - posredni poziom
    sciagania dla norm i referencji. CSV, pierwszy tag = klasa glowna."""
    with connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(champion)")}
        if "tags" not in cols:
            con.execute("ALTER TABLE champion ADD COLUMN tags TEXT")


def save_champions(champs):
    """champs: krotki (id, name, key) lub (id, name, key, tags_csv)."""
    with connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO champion (id, name, key, tags) VALUES (?, ?, ?, ?)",
            [c if len(c) == 4 else (*c, None) for c in champs])


def upgrade_trade_ids():
    """Cudze picki zostaja w puli (decyzja 31.08 - wymiana dziala), ale
    dostaja oznaczenie: osobna lista trade_ids obok champion_ids."""
    with connect() as con:
        for table in ("lobby", "champ_select_pool"):
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if cols and "trade_ids" not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN trade_ids TEXT")


def champion_classes():
    """champion_id -> klasa glowna (pierwszy tag DD) albo None."""
    with connect() as con:
        return {r["id"]: ((r["tags"] or "").split(",")[0] or None)
                for r in con.execute("SELECT id, tags FROM champion")}


def champion_count():
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM champion").fetchone()["c"]


def champion_lookup():
    """(G) Mapy do dopasowania zrodel zewnetrznych: slug = klucz DD lowercase
    (id="patch-aurelionsol" w notkach Riota) i nazwa znormalizowana (sekcje
    trybow w notkach nazywaja championow tekstem, np. Kog'Maw)."""
    def norm(x):
        return re.sub(r"[^a-z0-9]", "", (x or "").lower())
    with connect() as con:
        rows = con.execute("SELECT id, name, key FROM champion").fetchall()
    return {"slug": {r["key"].lower(): r["id"] for r in rows if r["key"]},
            "norm": {**{norm(r["name"]): r["id"] for r in rows if r["name"]},
                     **{norm(r["key"]): r["id"] for r in rows if r["key"]}}}


def champion_key(champion_id):
    """Klucz Data Dragona (np. MonkeyKing dla Wukonga) - takze slug
    stron buildow arammayhem po zlowercase'owaniu."""
    with connect() as con:
        r = con.execute("SELECT key FROM champion WHERE id=?",
                        (champion_id,)).fetchone()
    return r["key"] if r else None


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
        # progi moga sie zmienic miedzy splitami - klucz (from_milestone,
        # split_id) sprawia, ze REPLACE nadpisuje wylacznie w obrebie
        # biezacego splitu, a historia progow zostaje (partia D: sam
        # from_milestone w kluczu kasowal drabinke poprzedniego splitu)
        con.executemany(
            "INSERT OR REPLACE INTO milestone_ladder "
            "(from_milestone, require_grades, games, reward_marks, bonus, observed_at, split_id) "
            "VALUES (:from_milestone, :require_grades, :games, :reward_marks, :bonus, "
            ":observed_at, :split_id)",
            list(rows.values()))


def get_ladder():
    """Drabinka biezacego splitu; swiezy split bez wierszy (learn_ladder
    jeszcze nie pobiegl) dostaje ostatnia znana - lepsza stara drabinka
    niz pusty ranking."""
    with connect() as con:
        sid = current_split_id()
        rows = con.execute("SELECT * FROM milestone_ladder WHERE split_id=?",
                           (sid,)).fetchall()
        if not rows:
            last = con.execute(
                "SELECT MAX(split_id) s FROM milestone_ladder").fetchone()["s"]
            if last is not None:
                rows = con.execute(
                    "SELECT * FROM milestone_ladder WHERE split_id=?",
                    (last,)).fetchall()
        return {
            r["from_milestone"]: {
                "require_grades": json.loads(r["require_grades"]),
                "games": r["games"],
                "reward_marks": r["reward_marks"],
                "bonus": bool(r["bonus"]),
            }
            for r in rows
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


def latest_snapshot_ts():
    with connect() as con:
        row = con.execute("SELECT MAX(taken_at) t FROM snapshot").fetchone()
    return row["t"] if row and row["t"] else None


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

def set_lobby(champion_ids, queue, pool_kind, ts, trade_ids=None):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO lobby "
            "(id, champion_ids, queue, pool_kind, updated_at, trade_ids) "
            "VALUES (1, :ids, :queue, :pool_kind, :ts, :trade)",
            {"ids": json.dumps(champion_ids), "queue": queue,
             "pool_kind": pool_kind, "ts": ts,
             "trade": json.dumps(sorted(trade_ids or []))})


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
        "trade_ids": json.loads(row["trade_ids"] or "[]"),
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
        "game_mode": effective_mode(info.get("gameMode"), info.get("queueId")),
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

# Tryb misji ma JEDNA kolejke matchmakingu. Custom game zglasza ten sam
# gameMode (C5, sonda 2.09: kolejka 3270 "ARAM: Mayhem", category Custom,
# queueRewards wylaczone - zero ocen i punktow maestrii), a cala filtracja
# w modelu/normach idzie po game_mode - dwie gry treningowe weszly przez
# to do norm jako pelnoprawne KIWI (audyt eksportu 2.09). Zamiast odrzucac
# (surowiec sie nie wyrzuca), zapis nadaje takim grom odrozniamy tryb;
# surowe queue_id zostaje w wierszu. Ograniczenie: Live Client (port 2999)
# nie zna queueId, wiec panel live w customie dalej pokaze referencje
# KIWI - ulotne, swiadomie zaakceptowane.
MODE_QUEUES = {"KIWI": 2400}


def effective_mode(game_mode, queue_id):
    q = MODE_QUEUES.get(game_mode)
    if q is not None and queue_id is not None and queue_id != q:
        return f"{game_mode}_CUSTOM"
    return game_mode


def upgrade_custom_modes():
    """Backfill trybu dla gier zapisanych przed filtrem per queueId
    (2 customy Sionem z 29.08 i 1.09 siedzialy w normach). Idempotentne:
    po przemianowaniu warunek juz nie lapie."""
    with connect() as con:
        for mode, qid in MODE_QUEUES.items():
            con.execute(
                "UPDATE match_player SET game_mode = game_mode || '_CUSTOM' "
                "WHERE game_mode = ? AND queue_id IS NOT NULL AND queue_id != ?",
                (mode, qid))


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

def save_lcu_game(g, my_puuid=None):
    """Mecz z historii LCU (stary format v4). Zwraca True jesli wiersz byl
    nowy. Listing wlasnej historii ma jednego uczestnika; pelna gra po ID
    (odzysk P6) ma dziesieciu - wtedy wlasny wiersz wybieramy po puuid."""
    parts = g.get("participants") or []
    if not parts:
        return False
    p = parts[0]
    if len(parts) > 1:
        ident = {i["participantId"]: i for i in (g.get("participantIdentities") or [])}
        if my_puuid:
            mine = [x for x in parts
                    if (ident.get(x["participantId"], {}).get("player")
                        or {}).get("puuid") == my_puuid]
        else:
            mine = [x for x in parts if ident.get(x["participantId"])]
        p = mine[0] if mine else parts[0]

    st = p.get("stats") or {}
    tl = p.get("timeline") or {}
    raw_cid = p.get("championId", 0)

    row = {
        "match_id": f"{g.get('platformId', 'EUW1')}_{g['gameId']}",
        "queue_id": g.get("queueId"),
        "game_mode": effective_mode(g.get("gameMode"), g.get("queueId")),
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


def save_lcu_participants(g, match_id, my_puuid=None):
    """(partia D) Pelna gra z odzysku P6 niesie statystyki i tozsamosci
    WSZYSTKICH 10 graczy - dokladnie to, co z eog karmi player_stat (normy)
    i match_participant (karta 9). own_slice wyrzucal 9/10 tego materialu
    bezpowrotnie (gra po odzysku znika z listy missing i nikt po nia nie
    wraca). Numeracja slotow = participantId z formatu v4. Zwraca liczbe
    uczestnikow ze statystykami."""
    parts = g.get("participants") or []
    if len(parts) <= 1:
        return 0
    ident = {i["participantId"]: (i.get("player") or {}).get("puuid")
             for i in (g.get("participantIdentities") or [])}
    mode = g.get("gameMode")
    written = 0
    with connect() as con:
        con.execute("DELETE FROM match_participant WHERE match_id=?", (match_id,))
        for p in parts:
            pid = p.get("participantId")
            if not pid:
                continue
            puuid = ident.get(pid)
            if puuid:
                con.execute(
                    "INSERT INTO match_participant "
                    "(match_id, participant_no, puuid, team_id) VALUES (?,?,?,?)",
                    (match_id, pid, puuid, p.get("teamId")))
            cid = normalize_champion_id(p.get("championId"), mode)
            is_local = 1 if (my_puuid and puuid == my_puuid) else 0
            for k, v in (p.get("stats") or {}).items():
                if isinstance(v, bool):
                    v = int(v)
                if not isinstance(v, (int, float)):
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO player_stat "
                    "(match_id, participant_no, champion_id, team_id, is_local, "
                    "stat_key, stat_value) VALUES (?,?,?,?,?,?,?)",
                    (match_id, pid, cid, p.get("teamId") or 0, is_local, k, v))
            written += 1
    return written


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


GRADE_RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS grade_raw (
    match_id    TEXT PRIMARY KEY,
    game_id     INTEGER NOT NULL,
    payload     BLOB NOT NULL,
    captured_at INTEGER NOT NULL
);
"""


def init_grade_raw():
    with connect() as con:
        con.executescript(GRADE_RAW_SCHEMA)


def save_grade_raw(updates, platform, ts):
    """Caly surowy blok champion-mastery-updates per mecz (wzorzec eog_raw).
    save_grade wyciaga kilkanascie pol, a jedyna kopia reszty zyla w dumpach
    agenta z rotacja 60 plikow - gdy Riot przebuduje system ocen (robil to
    z mechanika trybu w 26.03 i 26.12), surowce sprzed zmiany maja istniec
    (raport 2.09, P3). Zwraca liczbe zapisanych meczow."""
    import zlib
    entries = [e for e in (updates if isinstance(updates, list) else [updates])
               if isinstance(e, dict)]
    by_gid = {}
    for e in entries:
        gid = e.get("gameId")
        if gid:
            by_gid.setdefault(gid, []).append(e)
    with connect() as con:
        for gid, ents in by_gid.items():
            con.execute(
                "INSERT OR REPLACE INTO grade_raw "
                "(match_id, game_id, payload, captured_at) VALUES (?,?,?,?)",
                (f"{platform.upper()}_{gid}", gid,
                 zlib.compress(json.dumps(ents, separators=(",", ":")).encode()),
                 ts))
    return len(by_gid)


def load_grade_raw(match_id):
    import zlib
    with connect() as con:
        row = con.execute("SELECT payload FROM grade_raw WHERE match_id=?",
                          (match_id,)).fetchone()
    return json.loads(zlib.decompress(row["payload"])) if row else None


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


# ---------- timelines (akwizycja, sonda C3) ----------
#
# /lol-match-history/v1/game-timelines/{gameId} oddaje dla kolejki 2400
# frames co ~60 s (totalGold/xp/level/pozycje WSZYSTKICH 10 graczy +
# eventy killi z asystami) - takze dla gier spoza okna 20 (sonda C3
# na najstarszej grze z bazy). Zbieramy caly surowiec wzorcem eog_raw
# (bez przycinania do wlasnego uczestnika: krzywe na tle lobby to istota);
# ZADNEJ analizy przed bramkami - pierwsze spojrzenie przy rewizji
# eventdata (50 gier), cechy tempa za bramka 60-100 obs.

TIMELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_timeline (
    match_id     TEXT PRIMARY KEY,
    game_id      INTEGER NOT NULL,
    frames       INTEGER,
    payload      BLOB NOT NULL,
    captured_at  INTEGER NOT NULL
);
"""


def init_timelines():
    with connect() as con:
        con.executescript(TIMELINE_SCHEMA)


def save_timeline(match_id, game_id, timeline, ts):
    """Caly obiekt timeline (skompresowany). Zwraca True dla nowego wpisu."""
    import zlib
    frames = timeline.get("frames") or []
    with connect() as con:
        existed = con.execute(
            "SELECT 1 FROM match_timeline WHERE match_id=?",
            (match_id,)).fetchone() is not None
        con.execute(
            "INSERT OR REPLACE INTO match_timeline "
            "(match_id, game_id, frames, payload, captured_at) "
            "VALUES (?,?,?,?,?)",
            (match_id, game_id, len(frames),
             zlib.compress(json.dumps(timeline, separators=(",", ":")).encode()),
             ts))
    return not existed


def load_timeline(match_id):
    import zlib
    with connect() as con:
        row = con.execute("SELECT payload FROM match_timeline WHERE match_id=?",
                          (match_id,)).fetchone()
    return json.loads(zlib.decompress(row["payload"])) if row else None


def missing_timelines(limit=5):
    """Gry misji bez zebranego timeline - agent dociaga je pojedynczo przy
    bezczynnym kliencie (druga reka petli odzysku P6). Najnowsze najpierw;
    customy (KIWI_CUSTOM) i inne tryby poza zakresem."""
    with connect() as con:
        return [r["gid"] for r in con.execute("""
            SELECT CAST(substr(m.match_id, instr(m.match_id, '_') + 1) AS INTEGER) gid
            FROM match_player m
            LEFT JOIN match_timeline t ON t.match_id = m.match_id
            WHERE m.game_mode = 'KIWI' AND t.match_id IS NULL
            ORDER BY m.game_creation DESC LIMIT ?""", (limit,))]


# ---------- tozsamosci graczy (karta 9) ----------
#
# flatten_eog_stats odrzuca pola nienumeryczne, wiec puuid ginal przy
# splaszczaniu - lacze match_id -> puuid zylo tylko w blobach eog_raw.
# Ta tabela utrwala je pod ta sama numeracja slotow co player_stat
# (kolejnosc druzyn i graczy w bloku), zeby JOIN byl trywialny.

MATCH_PARTICIPANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_participant (
    match_id       TEXT NOT NULL,
    participant_no INTEGER NOT NULL,
    puuid          TEXT NOT NULL,
    team_id        INTEGER,
    PRIMARY KEY (match_id, participant_no)
);

CREATE INDEX IF NOT EXISTS idx_mpart_puuid ON match_participant(puuid);
"""


def init_match_participant():
    with connect() as con:
        con.executescript(MATCH_PARTICIPANT_SCHEMA)


def save_match_participants(block, match_id):
    """Tozsamosci graczy z bloku eog -> match_participant. Gracz bez puuid
    (np. bot) nie dostaje wiersza, ale zajmuje slot - numeracja musi zostac
    zgodna z player_stat. Zwraca liczbe zapisanych wierszy."""
    rows = []
    n = 0
    for team in block.get("teams") or []:
        team_id = team.get("teamId")
        for p in team.get("players") or []:
            n += 1
            puuid = p.get("puuid")
            if not puuid:
                continue
            rows.append({"match_id": match_id, "participant_no": n,
                         "puuid": puuid, "team_id": team_id})
    if not rows:
        return 0
    with connect() as con:
        con.execute("DELETE FROM match_participant WHERE match_id=?", (match_id,))
        con.executemany(
            "INSERT INTO match_participant "
            "(match_id, participant_no, puuid, team_id) "
            "VALUES (:match_id, :participant_no, :puuid, :team_id)", rows)
    return len(rows)


def backfill_participants_from_eog():
    """Odzyskuje tozsamosci z zachowanych blobow eog_raw - gry sprzed
    patcha, w ktorych lacze zginelo przy splaszczaniu. Idempotentne:
    save_match_participants nadpisuje wiersze meczu w calosci."""
    with connect() as con:
        mids = [r["match_id"] for r in con.execute("SELECT match_id FROM eog_raw")]
    filled = empty = rows = 0
    for mid in mids:
        n = save_match_participants(load_eog(mid) or {}, mid)
        rows += n
        if n:
            filled += 1
        else:
            empty += 1
    out = {"blobs": len(mids), "filled": filled, "empty": empty, "rows": rows}
    log_event("participant_backfill", out)
    return out


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


def split_recap(mode=None):
    """(16) Podsumowanie biezacego splitu - z chwila resetu staje sie
    "wrapped" poprzedniego (zakres: od started_at ostatniego splitu)."""
    with connect() as con:
        row = con.execute(
            "SELECT started_at FROM split ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        since = row["started_at"] if row else 0
        clause = "AND game_mode = :mode" if mode else ""
        prm = {"since": since}
        if mode:
            prm["mode"] = mode
        norm_ts = ("(CASE WHEN game_creation > 1000000000000 "
                   "THEN game_creation / 1000 ELSE game_creation END)")
        gp = con.execute(f"""
            SELECT COUNT(*) games, SUM(win) wins, SUM(duration) secs,
                   COUNT(DISTINCT champion_id) champs
            FROM match_player
            WHERE duration > 300 AND {norm_ts} >= :since {clause}""",
            prm).fetchone()
        grades = [r["grade"] for r in con.execute(
            "SELECT grade FROM grade_observation WHERE observed_at >= :since",
            {"since": since}) if not r["grade"].startswith(">=")]
        top = con.execute(f"""
            SELECT champion_id, COUNT(*) n FROM match_player
            WHERE duration > 300 AND {norm_ts} >= :since {clause}
            GROUP BY champion_id ORDER BY n DESC LIMIT 1""", prm).fetchone()
    dist = {}
    for g in grades:
        dist[g] = dist.get(g, 0) + 1
    return {
        "since": since,
        "games": gp["games"] or 0,
        "wins": gp["wins"] or 0,
        "hours": round((gp["secs"] or 0) / 3600, 1),
        "unique_champions": gp["champs"] or 0,
        "grades": dist,
        "s_count": sum(v for g, v in dist.items() if g.startswith("S")),
        "a_count": sum(v for g, v in dist.items() if g.startswith("A")),
        "top_champion_id": top["champion_id"] if top else None,
        "top_champion_games": top["n"] if top else 0,
    }


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
    split_id      INTEGER,
    trade_ids     TEXT
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


def save_pool(champion_ids, queue, queue_id, pool_kind, ts, trade_ids=None):
    """Zapisuje pule z champ selecta. Nie duplikuje, jesli ta sama pula
    zostala juz zapisana i nie jest jeszcze przypisana do meczu."""
    if not champion_ids:
        return None
    ids_json = json.dumps(sorted(champion_ids))
    trade_json = json.dumps(sorted(trade_ids or []))
    with connect() as con:
        last = con.execute(
            "SELECT id, champion_ids, trade_ids FROM champ_select_pool "
            "WHERE match_id IS NULL ORDER BY ts DESC LIMIT 1").fetchone()
        if last and last["champion_ids"] == ids_json:
            # rotacja z lawka: unia bez zmian, przydzial inny - wiersz zostaje
            # ten sam, ale trade_ids musi dogonic stan; inaczej historia puli
            # mrozi sie na poczatku lobby, mimo ze lobby/UI dostaje swieze
            # plakietki (agent od 7573bde wysyla kazda rotacje)
            if last["trade_ids"] != trade_json:
                con.execute("UPDATE champ_select_pool SET trade_ids=? WHERE id=?",
                            (trade_json, last["id"]))
            return last["id"]
        return con.execute("""
            INSERT INTO champ_select_pool
              (ts, queue, queue_id, pool_kind, champion_ids, pool_size, split_id,
               trade_ids)
            VALUES (:ts, :queue, :queue_id, :pool_kind, :ids, :size, :split, :trade)
        """, {"ts": ts, "queue": queue, "queue_id": queue_id, "pool_kind": pool_kind,
              "ids": ids_json, "size": len(champion_ids),
              "split": current_split_id(),
              "trade": trade_json}).lastrowid


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


def _orphan_pool_matches(con, max_age=14400):
    """(H) Gry trybu misji bez przypietej puli, dla ktorych istnieje
    kandydatka: ostatnia niezlinkowana pula tej samej kolejki z okna max_age
    przed startem gry - ta sama regula co link_pool_to_match na zywo.
    Zrodlo problemu: link_pool_to_match wola wylacznie /eog, wiec gra, ktorej
    koniec agent przegapil (weszla z historii albo odzysku P6), zostawiala
    pule bez meczu, a predykcja sprzed gry wisiala w nieskonczonosc
    (kopia 4.09: Ezreal 31.08 z okna "grano bez agenta"). Tryb i czas gry
    jak w czujce ocen: Practice Tool i Classic po dodge'u Mayhema to nie
    przeciek. Zwraca [(match_id, champion_id, pool_id)]."""
    modes = ", ".join(f"'{m}'" for m in MODE_QUEUES)
    games = con.execute(f"""
        SELECT m.match_id, m.champion_id, m.queue_id,
               m.game_creation / 1000 AS start
        FROM match_player m
        WHERE m.game_mode IN ({modes}) AND m.game_creation IS NOT NULL
          AND COALESCE(m.duration, 999999) >= {REMAKE_MAX_S}
          AND m.match_id NOT IN (SELECT match_id FROM champ_select_pool
                                 WHERE match_id IS NOT NULL)
        ORDER BY m.game_creation""").fetchall()
    out, taken = [], set()
    for g in games:
        row = con.execute("""
            SELECT id FROM champ_select_pool
            WHERE match_id IS NULL AND ts BETWEEN ? AND ?
              AND (queue_id IS NULL OR queue_id = ?)
            ORDER BY ts DESC LIMIT 1""",
            (g["start"] - max_age, g["start"] + 60, g["queue_id"])).fetchone()
        if row and row["id"] not in taken:
            taken.add(row["id"])
            out.append((g["match_id"], g["champion_id"], row["id"]))
    return out


def link_orphan_pools(max_age=14400):
    """Dopina zalegle pule do gier z historii/odzysku (patrz
    _orphan_pool_matches); picked_id = champion z match_player, przez co
    prediction_pairs rozstrzyga predykcje samo. Idempotentne."""
    with connect() as con:
        pairs = _orphan_pool_matches(con, max_age)
        for match_id, champion_id, pool_id in pairs:
            con.execute(
                "UPDATE champ_select_pool SET picked_id=?, match_id=? WHERE id=?",
                (champion_id, match_id, pool_id))
    return len(pairs)


def median_final_pool_size():
    """Mediana FINALNYCH pul kartowych. Walidacja 1.09: tabela miesza stany
    czesciowe (naplyw kart, 2-6 championow) i pule "full" (173) - filtr:
    limited + zlinkowane z meczem (link_pool_to_match bierze ostatni stan
    przed gra). Fallback dla swiezej bazy: limited >= 8, potem stala 11."""
    import statistics
    with connect() as con:
        rows = [r["pool_size"] for r in con.execute(
            "SELECT pool_size FROM champ_select_pool "
            "WHERE pool_kind='limited' AND match_id IS NOT NULL AND pool_size>0")]
        if not rows:
            rows = [r["pool_size"] for r in con.execute(
                "SELECT pool_size FROM champ_select_pool "
                "WHERE pool_kind='limited' AND pool_size>=8")]
    return int(statistics.median(rows)) if rows else 11


def champion_sb_popularity():
    """Ile gier snowballa widzielismy per champion - proxy popularnosci
    w populacji Mayhema (walidacja 1.09: rho=-0.583 z resztami modelu)."""
    with connect() as con:
        # GLOB zamiast LIKE: przy domyslnej kolacji LIKE nie schodzil do
        # zakresu indeksu (PK zaczyna sie od match_id) i skanowal cala
        # najwieksza tabele bazy - per request /targets, co 4 s przy
        # otwartej karcie (audyt 2.09)
        return {r["champion_id"]: r["n"] for r in con.execute(
            "SELECT champion_id, COUNT(DISTINCT match_id) n FROM player_stat "
            "WHERE match_id GLOB 'SB_*' GROUP BY champion_id")}


def games_on_patch(patch_short, mode=None):
    clause = "AND game_mode = ?" if mode else ""
    args = (patch_short,) + ((mode,) if mode else ())
    with connect() as con:
        return con.execute(
            f"SELECT COUNT(*) c FROM match_player WHERE patch = ? {clause}",
            args).fetchone()["c"]


def recent_tempo(mode=None, days=7):
    """Gry na dzien w ostatnich `days` dniach - paliwo projekcji deadline'u
    przepustki. game_creation bywa w ms i w s - normalizujemy."""
    cutoff = time.time() - days * 86400
    clause = "WHERE game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        ts = [r["game_creation"] for r in con.execute(
            f"SELECT game_creation FROM match_player {clause}", args)]
    n = sum(1 for t in ts
            if t and (t / 1000 if t > 10**12 else t) >= cutoff)
    return round(n / days, 2)


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
            # Itemy koncowe: w bloku eog leza jako LISTA na graczu, nie jako
            # skalar w stats - petla wyzej je pomijala i buildy wlasnych gier
            # przepadaly. Snowball itemy lapie od zawsze (historia LCU trzyma
            # item0..item6 w stats i tam nie ma whitelisty).
            for i, it in enumerate((p.get("items") or [])[:7]):
                iid = it.get("itemId") if isinstance(it, dict) else it
                if isinstance(iid, (int, float)) and iid:
                    rows.append({
                        "match_id": match_id,
                        "participant_no": n,
                        "champion_id": p.get("championId"),
                        "team_id": team_id,
                        "is_local": is_local,
                        "stat_key": f"item{i}",
                        "stat_value": float(iid),
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


# (E) Percentyl wewnatrzmeczowy: pozycja wsrod 10 graczy TEGO meczu.
# Kontekst, nie diagnoza - pozycja jest skonfundowana skladem druzyn
# (mag nie "dokreci" przetankowania wzgledem tanka), a ocena Riota liczy
# sie wzgledem populacji championa; UI podpisuje to wprost.
PCT_KEYS = [
    ("totalDamageDealtToChampions", "obrażenia"),
    ("totalDamageTaken", "obrażenia przyjęte"),
    ("totalHeal", "leczenie"),
    ("goldEarned", "złoto"),
]


def match_percentiles(match_id):
    """Rozszerzenie my_share na kilka kluczy per-min naraz. Wylacznie mecze
    z pelnym lobby (>=8 uczestnikow ze statystykami): wpisy jednoosobowe
    (wlasny listing LCU, stare SB) pokazywalyby '1. z 1'."""
    from .features import minutes
    keys = [k for k, _ in PCT_KEYS]
    marks = ",".join("?" * len(keys))
    with connect() as con:
        dur = con.execute("SELECT duration FROM match_player WHERE match_id=?",
                          (match_id,)).fetchone()
        rows = [dict(r) for r in con.execute(
            f"SELECT participant_no, is_local, stat_key, stat_value "
            f"FROM player_stat WHERE match_id=? AND stat_key IN ({marks})",
            (match_id, *keys))]
    if not dur or len({r["participant_no"] for r in rows}) < 8:
        return []
    mins = minutes(dur["duration"])
    out = []
    for key, label in PCT_KEYS:
        vals = [(r["stat_value"], r["is_local"]) for r in rows
                if r["stat_key"] == key]
        mine = next((v for v, loc in vals if loc), None)
        if mine is None or len(vals) < 8:
            continue
        better = sum(1 for v, _ in vals if v > mine)
        out.append({"key": key, "label": label, "rank": better + 1,
                    "of": len(vals), "per_min": round(mine / mins, 1)})
    return out


def agent_activity_gaps(limit=5, slack=300):
    """(E) Watchdog "grano bez agenta": miedzy snapshotami punkty maestrii
    urosly, a agent nie zameldowal ZADNEGO ekranu koncowego w tym oknie.
    Gry sa odzyskiwalne (P6/backfill), bezpowrotnie przepadaja oceny sprzed
    dosylki, live i eventdata - baner ma wywolac odzysk, zanim gra wypadnie
    z okna 20. Okna po timestampach snapshotow (nie dobach kalendarzowych -
    pulapka UTC), z luzem na wyscigi zapisu."""
    with connect() as con:
        snaps = [dict(r) for r in con.execute(
            "SELECT id, taken_at FROM snapshot ORDER BY taken_at")]
        totals = {r["snapshot_id"]: r["p"] for r in con.execute(
            "SELECT snapshot_id, SUM(points) p FROM mastery "
            "GROUP BY snapshot_id")}
        eog_ts = [r["ts"] for r in con.execute(
            "SELECT ts FROM event_log WHERE kind='eog'")]
    gaps = []
    for a, b in zip(snaps, snaps[1:], strict=False):
        delta = (totals.get(b["id"]) or 0) - (totals.get(a["id"]) or 0)
        if delta <= 0:
            continue
        lo, hi = a["taken_at"] - slack, b["taken_at"] + slack
        if any(lo <= t <= hi for t in eog_ts):
            continue
        gaps.append({"from_ts": a["taken_at"], "to_ts": b["taken_at"],
                     "points_delta": delta})
    return gaps[-limit:]


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


def data_gates():
    """(P4) Liczniki bramek danych ze STAN.md - "sprawdzać liczniki" ma
    znaczyc "spojrzec na System", nie "policzyc recznie w bazie". Progi sa
    orientacyjne (~), definicje bramek zyja w STAN.md - tu tylko odczyt."""
    s_rank = GRADE_RANK["S-"]
    with connect() as con:
        exact = con.execute(
            "SELECT COUNT(*) c FROM grade_observation "
            "WHERE COALESCE(censored,0)=0").fetchone()["c"]
        s_pos = sum(1 for r in con.execute(
            "SELECT grade FROM grade_observation WHERE COALESCE(censored,0)=0")
            if GRADE_RANK.get(r["grade"], -1) >= s_rank)
        eventdata = con.execute(
            "SELECT COUNT(*) c FROM live_event_log").fetchone()["c"]
        usable = con.execute("""
            SELECT COUNT(*) c FROM grade_observation g
            JOIN match_player m ON m.match_id = g.match_id""").fetchone()["c"]
    resolved, _pending = prediction_pairs()
    return [
        {"key": "s_minus", "label": "Model S- (dokładne pozytywy)",
         "have": s_pos, "need": 5},
        {"key": "brier", "label": "Brier interpretowalny (pary predykcji)",
         "have": len(resolved), "need": 20},
        {"key": "fatigue", "label": "Hipoteza zmęczenia (dokładne oceny)",
         "have": exact, "need": 40},
        {"key": "eventdata", "label": "Rewizja eventdata (gry z logiem)",
         "have": eventdata, "need": 50},
        {"key": "class_feats", "label": "Cechy klasowe (obserwacje)",
         "have": usable, "need": 60},
        {"key": "big_review", "label": "Rewizja duża: ranking/CUSUM/kalibracja",
         "have": usable, "need": 100},
    ]


# Ponizej tylu sekund gra to remake/void - Riot nie daje za nia oceny ani
# maestrii (remake konczy sie ok. 3-3.5 min).
REMAKE_MAX_S = 300


def _eog_no_grade_sql(cols):
    """(A6/F3) jedyna czujka na smierc kanalu ocen: ekran koncowy jest, oceny
    nie ma. Rosnaca liczba = patch Riota zabil kanal albo epizod pomeczowy
    przegrywa wyscig - a ocena to jedyna strata bezpowrotna calego systemu.
    Liczymy WYLACZNIE gry trybu misji: customy (queueRewards wylaczone, sonda
    C5) i tryby wykluczone (JADE) nie maja oceny z definicji - pierwsza
    wersja odsiewala tylko customy i na produkcji zostal JADE 350 s
    (EUW1_7969869213, 4.09). Ekran bez wiersza meczu liczymy ostroznie, bo
    trybu nie znamy."""
    modes = ", ".join(f"'{m}'" for m in MODE_QUEUES)
    return f"""
    SELECT {cols} FROM eog_raw e
    LEFT JOIN grade_observation g ON g.match_id = e.match_id
    LEFT JOIN match_player m ON m.match_id = e.match_id
    WHERE g.match_id IS NULL
      AND (m.game_mode IS NULL OR m.game_mode IN ({modes}))
      AND COALESCE(m.duration, 999999) >= {REMAKE_MAX_S}"""


def pipeline_sanity():
    """(P8) Dziury w potoku, w dwoch grupach (F3, 3.09). ALARMOWE - kazda
    niezerowa liczba to realny przeciek: ocena bez meczu, ekran bez
    tozsamosci, ekran gry misji bez oceny, pula z nieprzypisana gra.
    INFORMACYJNE - pula bez zadnej gry to dodge/remake/trening (poprawne
    dzialanie), a brakujace statystyki i timeline'y agent dociaga sam.
    Jeden napis "wszystko powyzej zera to przeciek" przy 28 dodge'ach
    wygladal jak awaria (zrzut z produkcji, 3.09)."""
    import time as _t
    cutoff = int(_t.time()) - 86400
    with connect() as con:
        orphan_grades = con.execute("""
            SELECT COUNT(*) c FROM grade_observation g
            LEFT JOIN match_player m ON m.match_id = g.match_id
            WHERE m.match_id IS NULL""").fetchone()["c"]
        eog_no_participants = con.execute("""
            SELECT COUNT(*) c FROM eog_raw e
            LEFT JOIN (SELECT DISTINCT match_id FROM match_participant) p
              ON p.match_id = e.match_id
            WHERE p.match_id IS NULL""").fetchone()["c"]
        stale_pools = con.execute(
            "SELECT COUNT(*) c FROM champ_select_pool "
            "WHERE match_id IS NULL AND ts < ?", (cutoff,)).fetchone()["c"]
        # prawdziwy przeciek pul: gra MISJI bez przypietej puli, choc pula
        # z jej kolejki byla tuz przed nia (pierwsza wersja liczyla pule
        # z DOWOLNA gra w 2 h - Practice Tool i Classic po dodge'u Mayhema
        # dawaly 9 falszywych alarmow, produkcja 4.09)
        games_unlinked_pool = len(_orphan_pool_matches(con))
        eog_no_grade = con.execute(
            _eog_no_grade_sql("COUNT(*) c")).fetchone()["c"]
    return {"orphan_grades": orphan_grades,
            "eog_no_participants": eog_no_participants,
            "stale_pools": stale_pools,
            "games_unlinked_pool": games_unlinked_pool,
            "eog_bez_oceny": eog_no_grade,
            # (P6) odzyskiwalne przez agenta - niezerowe topnieje samo
            "missing_games": len(missing_own_games(1000)),
            # timelines: jak wyzej, druga reka petli odzysku
            "timeline_missing": len(missing_timelines(1000))}


def eog_without_grade_ids(limit=5):
    """Ktore gry misji maja ekran koncowy bez oceny - sam licznik nie mowi,
    czy to remake sprzed minuty, czy kanal ocen martwy od tygodnia."""
    with connect() as con:
        return [r["match_id"] for r in con.execute(
            _eog_no_grade_sql("e.match_id")
            + " ORDER BY e.captured_at DESC, e.match_id LIMIT ?", (limit,))]


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


def reference_pace(threshold="A-", mode=None, champion_id=None):
    """Tempo z gier, ktore skonczyly sie ocena >= progu. Punkt odniesienia
    dla overlaya: 'tak wygladaly Twoje udane gry'.

    Drabinka zakresu: gry na TYM championie -> gry na jego klasie (tagi DD)
    -> wszystkie gry. Wczesniej support byl porownywany do mediany
    zdominowanej przez carry i zawsze wygladal na obiboka. Kazdy szczebel
    wymaga REF_MIN_HITS trafien; bez champion_id dziala po staremu."""
    want = GRADE_RANK.get(threshold)
    clause = "AND m.game_mode = ?" if mode else ""
    args = (mode,) if mode else ()
    with connect() as con:
        rows = [dict(r) for r in con.execute(f"""
            SELECT g.grade, g.champion_id, m.kills, m.deaths, m.assists,
                   m.cs, m.gold, m.duration
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
        (hit if rank >= want else miss).append((r["champion_id"], vals))

    scope, label = "global", None
    h_use = [v for _, v in hit]
    m_use = [v for _, v in miss]
    if champion_id:
        classes = champion_classes()
        own_h = [v for c, v in hit if c == champion_id]
        if len(own_h) >= REF_MIN_HITS:
            scope, h_use = "champion", own_h
            m_use = [v for c, v in miss if c == champion_id]
        else:
            cls = classes.get(champion_id)
            cls_h = [v for c, v in hit if cls and classes.get(c) == cls]
            if len(cls_h) >= REF_MIN_HITS:
                scope, label, h_use = "class", cls, cls_h
                m_use = [v for c, v in miss if classes.get(c) == cls]

    def med(rows_, key):
        v = [x[key] for x in rows_]
        return round(statistics.median(v), 2) if v else None

    keys = ["ka_per_min", "cs_per_min", "deaths_per_min", "gold_per_min"]
    return {
        "threshold": threshold,
        "scope": scope,
        "scope_label": label,
        "hit_games": len(h_use),
        "miss_games": len(m_use),
        "hit": {k: med(h_use, k) for k in keys},
        "miss": {k: med(m_use, k) for k in keys},
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
CLASS_MIN_OBS = 5   # tyle obserwacji musi miec klasa, zeby byc kotwica
REF_MIN_HITS = 3    # tyle trafien wymaga szczebel referencji (champion/klasa)


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

    # Posredni poziom sciagania: klasa championa (tagi Data Dragona).
    # Champion z 2 grami pozycza statystyki od podobnych postaci, nie od
    # calej populacji - tank przestaje byc sciagany do sredniej carry.
    # Bez tagow w bazie (przed refreshem DD) anchors sa puste i wszystko
    # dziala jak dotychczas: sciaganie prosto do globalu.
    classes = champion_classes()
    by_class = {}
    for cid, vals in per.items():
        cls = classes.get(cid)
        if cls:
            by_class.setdefault(cls, []).extend(vals)
    anchors = {cls: (statistics.mean(v), statistics.pstdev(v) or g_sd)
               for cls, v in by_class.items() if len(v) >= CLASS_MIN_OBS}

    out = {}
    for cid, vals in per.items():
        n = len(vals)
        if n < min_obs:
            continue
        m = statistics.mean(vals)
        sd = statistics.pstdev(vals) if n > 1 else g_sd
        cls = classes.get(cid)
        a_mean, a_sd = anchors.get(cls, (g_mean, g_sd))
        w = n / (n + NORM_SHRINK)
        out[cid] = {
            "n": n,
            "mean_raw": round(m, 2),
            "mean": round(w * m + (1 - w) * a_mean, 2),
            "sd": round(max(w * sd + (1 - w) * a_sd, g_sd * 0.3), 2),
            "confidence": round(w, 2),
            "class": cls,
            "anchor": "klasa" if cls in anchors else "global",
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
    next_p      REAL,
    specific    INTEGER DEFAULT 0,
    own_games   INTEGER,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (pool_id, champion_id)
);
"""


def save_pool_predictions(pool_id, rows, ts):
    with connect() as con:
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO pool_prediction "
                "(pool_id, champion_id, threshold, p, next_p, specific, "
                "own_games, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (pool_id, r["champion_id"], r.get("next_grade"), r.get("model_p"),
                 r.get("next_p"),
                 1 if r.get("model_specific") else 0, r.get("model_own_games"), ts))
    return len(rows)


def prediction_pairs():
    """Predykcje sprzed gry sparowane z tym, co faktycznie wypadlo.
    Tylko champion, ktorego naprawde wybrano - reszta puli nie ma wyniku.
    (B1) Obok p modelu takze next_p z czestosci - to ONO steruje E(c)
    i wyborem championa, wiec pary istnieja rowniez tam, gdzie model
    swiadomie milczy (S- z p=None)."""
    with connect() as con:
        resolved = [dict(r) for r in con.execute("""
            SELECT pp.p, pp.next_p, pp.threshold, pp.specific, g.grade, csp.ts
            FROM pool_prediction pp
            JOIN champ_select_pool csp
              ON csp.id = pp.pool_id AND csp.picked_id = pp.champion_id
            JOIN grade_observation g ON g.match_id = csp.match_id
            WHERE pp.p IS NOT NULL OR pp.next_p IS NOT NULL
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


def upgrade_predictions_next_p():
    """(B1, przeglad 2.09) Scorecard walidowal wylacznie p modelu
    porzadkowego, a championa wybiera E(c) na czestosciach champion_rates -
    "jedyny test, ktorego nie da sie oszukac" testowal niewlasciwy
    estymator. next_p trzyma p szczebla z czestosci w chwili predykcji."""
    with connect() as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(pool_prediction)")]
        if "next_p" not in cols:
            con.execute("ALTER TABLE pool_prediction ADD COLUMN next_p REAL")


# ============================================================
#  Konsola LCU (karta 42) + odzysk gier po ID (P6)
# ============================================================
#
# LCU widzi tylko agent na Windows, a sondy chce sie zlecac z UI - stad
# kolejka w bazie: UI tworzy zlecenie, agent odpytuje /probe/pending co
# pare sekund, wykonuje surowy GET i odklada wynik. WYLACZNIE odczyty -
# zadnych zapisow do LCU (decyzja: zero automatyzacji picków).

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lcu_probe (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    requested_at INTEGER NOT NULL,
    answered_at  INTEGER,
    http_status  INTEGER,
    response     TEXT,
    truncated    INTEGER DEFAULT 0
);
"""

PROBE_KEEP = 50            # sondy to narzedzie diagnostyczne, nie archiwum
PROBE_MAX_RESPONSE = 100_000


def init_probe():
    with connect() as con:
        con.executescript(PROBE_SCHEMA)


def probe_create(path, ts):
    with connect() as con:
        pid = con.execute(
            "INSERT INTO lcu_probe (path, requested_at) VALUES (?,?)",
            (path, ts)).lastrowid
        con.execute(
            "DELETE FROM lcu_probe WHERE id NOT IN "
            "(SELECT id FROM lcu_probe ORDER BY id DESC LIMIT ?)",
            (PROBE_KEEP,))
    return pid


def probe_pending(limit=5):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, path FROM lcu_probe WHERE answered_at IS NULL "
            "ORDER BY id ASC LIMIT ?", (limit,))]


def probe_answer(pid, http_status, response, ts):
    truncated = 0
    if response and len(response) > PROBE_MAX_RESPONSE:
        response = response[:PROBE_MAX_RESPONSE]
        truncated = 1
    with connect() as con:
        con.execute(
            "UPDATE lcu_probe SET answered_at=?, http_status=?, response=?, "
            "truncated=? WHERE id=?", (ts, http_status, response, truncated, pid))


def probe_get(pid):
    with connect() as con:
        r = con.execute("SELECT * FROM lcu_probe WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def missing_own_games(limit=10):
    """(P6) Gry, o ktorych system wie (eog, ocena albo domknieta pula),
    a nie maja statystyk w match_player - okno 20 historii LCU je
    przeoczylo, bo agent nie dzialal. Pojedyncza gre da sie pobrac po ID
    w komplecie, dowolnie stara (README) - agent dociaga je z tej listy."""
    with connect() as con:
        return [r["gid"] for r in con.execute("""
            SELECT DISTINCT gid FROM (
                SELECT game_id AS gid, match_id AS mid FROM eog_raw
                UNION
                SELECT game_id, match_id FROM grade_observation
                UNION
                SELECT CAST(substr(match_id, instr(match_id, '_') + 1) AS INTEGER),
                       match_id
                FROM champ_select_pool WHERE match_id IS NOT NULL
            )
            WHERE mid NOT IN (SELECT match_id FROM match_player)
            ORDER BY gid DESC LIMIT ?""", (limit,))]


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
    """Gracze do sprawdzenia: nigdy nie sprawdzani albo starsi niz okno.
    (P7) Priorytet: gracze widziani w wielu wlasnych meczach
    (match_participant) - ich historie karmia normy i karte 9 najszybciej;
    reszta po staremu, od najdawniej sprawdzanych."""
    cutoff = int(time.time()) - SNOWBALL_REVISIT_DAYS * 86400
    with connect() as con:
        return [r["puuid"] for r in con.execute("""
            SELECT s.puuid FROM snowball_seen s
            LEFT JOIN (SELECT puuid, COUNT(*) n FROM match_participant
                       GROUP BY puuid) mp ON mp.puuid = s.puuid
            WHERE s.checked_at < ?
            ORDER BY COALESCE(mp.n, 0) DESC, s.checked_at ASC, s.added_at ASC
            LIMIT ?""", (cutoff, limit))]


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

CREATE TABLE IF NOT EXISTS snowball_pair (
    game_id  INTEGER NOT NULL,
    puuid    TEXT NOT NULL,
    PRIMARY KEY (game_id, puuid)
);
"""


def init_snowball_match():
    with connect() as con:
        con.executescript(SNOWBALL_MATCH_SCHEMA)


def upgrade_snowball_pairs():
    """(partia D) Dedup po samym game_id odrzucal obserwacje INNEGO gracza
    z tej samej gry (duet A+B: historie mocno sie nakladaja, polowa
    materialu przepadala z projektu, nie z przypadku). Para (gra, gracz)
    w osobnej tabeli; snowball_match zostaje 1 wiersz na gre, wiec widok
    norm_source nie duplikuje. Backfill: pierwszy obserwator z from_puuid."""
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO snowball_pair (game_id, puuid) "
            "SELECT game_id, from_puuid FROM snowball_match "
            "WHERE from_puuid IS NOT NULL")


LIVE_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  saved_at INTEGER NOT NULL,
  champion_id INTEGER,
  events TEXT NOT NULL
);
"""


def init_live_event_log():
    with connect() as con:
        con.executescript(LIVE_EVENT_SCHEMA)


def save_live_events(champion_id, events):
    """Surowy log zdarzen Live Client (kille/zgony/wieze z timestampami).
    Zbierany od 1.09: zyje tylko z portem 2999, wiec kazda gra bez zapisu
    to bezpowrotna strata. Analiza metryk timingu - po ~50 grach albo
    zbieranie wypada (bramka rewizyjna z backlogu)."""
    with connect() as con:
        con.execute(
            "INSERT INTO live_event_log (saved_at, champion_id, events) "
            "VALUES (?,?,?)",
            (int(time.time()), champion_id, json.dumps(events)))
    log_event("eventdata", {"events": len(events), "champion_id": champion_id})


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
            # wylacznie kolejka matchmakingu misji - warunek "albo mode
            # KIWI" wpuszczal customy obcych graczy (3270) do norm
            if not gid or qid != MODE_QUEUES["KIWI"]:
                continue
            kiwi += 1
            dur = int(g.get("gameDuration") or 0)
            if dur > 10000:          # niektore zrodla daja milisekundy
                dur //= 1000
            if dur <= 300:
                continue
            own = con.execute(
                "SELECT 1 FROM match_player WHERE match_id LIKE ? ESCAPE '\\'",
                (f"%\\_{gid}",)).fetchone()
            if own:
                continue
            con.execute(
                "INSERT OR IGNORE INTO snowball_match "
                "(game_id, duration, game_mode, queue_id, game_ts, from_puuid) "
                "VALUES (?,?,?,?,?,?)",
                (gid, dur, mode, qid, int((g.get("gameCreation") or 0) / 1000),
                 puuid))
            # dedup po PARZE (gra, gracz): ta sama gra widziana od drugiego
            # gracza to INNY uczestnik i nowa obserwacja norm - dedup po
            # samym game_id wyrzucal ja bezpowrotnie (partia D)
            cur = con.execute(
                "INSERT OR IGNORE INTO snowball_pair (game_id, puuid) "
                "VALUES (?,?)", (gid, puuid))
            if cur.rowcount == 0:    # ta gra od TEGO gracza juz zmielona
                continue
            part = (g.get("participants") or [{}])[0]
            cid = normalize_champion_id(part.get("championId"), mode)
            stats = part.get("stats") or {}
            mid = f"SB_{gid}"
            pn = con.execute(
                "SELECT COALESCE(MAX(participant_no), 0) + 1 n "
                "FROM player_stat WHERE match_id=?", (mid,)).fetchone()["n"]
            for k, v in stats.items():
                if isinstance(v, bool):
                    v = int(v)
                if not isinstance(v, (int, float)):
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO player_stat "
                    "(match_id, participant_no, champion_id, team_id, is_local, "
                    "stat_key, stat_value) VALUES (?,?,?,?,0,?,?)",
                    (mid, pn, cid, part.get("teamId") or 0, k, v))
                new_rows += 1
    return kiwi, new_rows


def upgrade_ladder_split_key():
    """(partia D) Stary PK milestone_ladder to sam from_milestone - REPLACE
    kasowal drabinke poprzedniego splitu, mimo ze kolumne split_id dodano
    wlasnie po to, by ja zachowac (a backfill cenzur ze starego splitu
    dostawal progi z nowego). SQLite nie zmienia PK w miejscu, wiec
    przebudowa: nowa tabela z kluczem (from_milestone, split_id), kopia,
    podmiana. Wykrycie po liczbie kolumn w kluczu - po przebudowie warunek
    juz nie lapie. Definicja na koncu pliku, bo migrate() wykonuje funkcje
    w kolejnosci definicji, a ta musi biec PO ALTER-ach z init_extra."""
    with connect() as con:
        pk_cols = con.execute(
            "SELECT COUNT(*) c FROM pragma_table_info('milestone_ladder') "
            "WHERE pk > 0").fetchone()["c"]
        if pk_cols != 1:
            return
        con.executescript("""
            CREATE TABLE milestone_ladder_new (
                from_milestone INTEGER NOT NULL,
                require_grades TEXT NOT NULL,
                games          INTEGER NOT NULL,
                reward_marks   INTEGER NOT NULL,
                bonus          INTEGER NOT NULL,
                observed_at    INTEGER NOT NULL,
                split_id       INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (from_milestone, split_id)
            );
            INSERT INTO milestone_ladder_new
                SELECT from_milestone, require_grades, games, reward_marks,
                       bonus, observed_at, COALESCE(split_id, 1)
                FROM milestone_ladder;
            DROP TABLE milestone_ladder;
            ALTER TABLE milestone_ladder_new RENAME TO milestone_ladder;
        """)


def upgrade_link_orphan_pools():
    """(H) Jednorazowe dopiecie zaleglych pul przy starcie - potem robi to
    /history/lcu przy kazdej nowej grze. Idempotentne, na pustej bazie
    testowej nic nie robi. Na koncu pliku: potrzebuje pelnego schematu."""
    link_orphan_pools()
