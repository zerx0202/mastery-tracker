#!/usr/bin/env python3
"""
Dry-run odtwarzalnosci (panel E): dowod, ze surowce (grade_raw, eog_raw)
odtwarzaja pochodne (grade_observation, player_stat, match_participant)
PRODUKCYJNYMI sciezkami zapisu - czyli ze archiwum blobow to polisa na
przebudowe systemu ocen przez Riota, a nie sama intencja. Kazdy rozjazd
znaleziony dzis to cichy bug ekstrakcji zatruwajacy dane treningowe.

Warunki z panelu (bez nich raport tonie w falszywych pozytywach):
  - porownujemy WYLACZNIE przeciecie pochodnych z istniejacymi surowcami;
    "brak surowca" (grade_raw istnieje od 2.09) raportowany osobno,
  - pola niedeterministyczne (observed_at, split_id, source, confidence)
    zamaskowane; replay idzie z captured_at surowca,
  - przed replayem save_grade baza tymczasowa dostaje match_player z kopii
    (normalize_champion_id -> _mode_of czyta tryb wlasnie stamtad),
  - baza tymczasowa przez podmiane db.DB_PATH - ta sama sciezka co
    fixture fresh_db, produkcyjne funkcje bez modyfikacji.

Uruchom na KOPII: python tools/dry_run_rebuild.py /sciezka/kopii.db
"""
import json
import sqlite3
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, "/code")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GRADE_FIELDS = ("grade", "champion_id", "score", "points_gained",
                "points_contrib", "points_before", "level_after",
                "tokens_earned")


def _src(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rebuild_and_compare(copy_path, tmp_db_path):
    from app import db as adb
    src = _src(copy_path)
    old_path = adb.DB_PATH
    adb.DB_PATH = Path(tmp_db_path)
    report = {}
    try:
        adb.migrate()
        # zaleznosc _mode_of: tryby meczow musza byc widoczne przy replayu
        mp_rows = [dict(r) for r in src.execute("SELECT * FROM match_player")]
        if mp_rows:
            cols = list(mp_rows[0].keys())
            with adb.connect() as con:
                con.executemany(
                    f"INSERT OR REPLACE INTO match_player ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})", mp_rows)

        # --- replay ocen z grade_raw ---
        graded = [dict(r) for r in src.execute(
            "SELECT match_id, payload, captured_at FROM grade_raw")]
        for r in graded:
            platform = r["match_id"].split("_")[0].lower()
            for e in json.loads(zlib.decompress(r["payload"])):
                adb.save_grade(e, platform, r["captured_at"])

        g_cmp = g_mis = 0
        g_diffs = []
        with adb.connect() as tmp:
            for r in graded:
                a = src.execute(
                    "SELECT * FROM grade_observation WHERE match_id=?",
                    (r["match_id"],)).fetchone()
                b = tmp.execute(
                    "SELECT * FROM grade_observation WHERE match_id=?",
                    (r["match_id"],)).fetchone()
                if a is None:
                    continue          # pochodna nie istnieje w kopii - nie rozjazd
                g_cmp += 1
                bad = [f for f in GRADE_FIELDS
                       if b is None or a[f] != b[f]]
                if bad:
                    g_mis += 1
                    g_diffs.append((r["match_id"], bad))
        report["grade"] = {"raw_blobs": len(graded), "compared": g_cmp,
                           "mismatch": g_mis, "diffs": g_diffs[:10]}

        # --- replay eog_raw -> eog/player_stat/match_participant ---
        eogs = [dict(r) for r in src.execute(
            "SELECT match_id, payload, captured_at FROM eog_raw")]
        for r in eogs:
            platform = r["match_id"].split("_")[0].lower()
            block = json.loads(zlib.decompress(r["payload"]))
            adb.save_eog(block, platform, r["captured_at"])
            adb.flatten_eog_stats(block, r["match_id"])
            adb.save_match_participants(block, r["match_id"])

        e_cmp = e_mis = ps_mis = part_mis = 0
        e_diffs = []
        with adb.connect() as tmp:
            for r in eogs:
                mid = r["match_id"]
                a = src.execute("SELECT champion_id, augments FROM eog_raw "
                                "WHERE match_id=?", (mid,)).fetchone()
                b = tmp.execute("SELECT champion_id, augments FROM eog_raw "
                                "WHERE match_id=?", (mid,)).fetchone()
                e_cmp += 1
                if b is None or tuple(a) != tuple(b):
                    e_mis += 1
                    e_diffs.append(mid)

                q = ("SELECT COUNT(*) c, COALESCE(SUM(stat_value),0) s "
                     "FROM player_stat WHERE match_id=? "
                     "AND match_id IN (SELECT match_id FROM eog_raw)")
                pa = src.execute(q, (mid,)).fetchone()
                pb = tmp.execute(q, (mid,)).fetchone()
                if (pa["c"], round(pa["s"], 3)) != (pb["c"], round(pb["s"], 3)):
                    ps_mis += 1

                ids_a = set(map(tuple, src.execute(
                    "SELECT participant_no, puuid FROM match_participant "
                    "WHERE match_id=?", (mid,))))
                ids_b = set(map(tuple, tmp.execute(
                    "SELECT participant_no, puuid FROM match_participant "
                    "WHERE match_id=?", (mid,))))
                if ids_a != ids_b:
                    part_mis += 1
        report["eog"] = {"raw_blobs": len(eogs), "compared": e_cmp,
                         "mismatch": e_mis, "diffs": e_diffs[:10]}
        report["player_stat"] = {"mismatch": ps_mis}
        report["participants"] = {"mismatch": part_mis}

        with adb.connect() as tmp:
            derived = src.execute(
                "SELECT COUNT(*) c FROM grade_observation WHERE match_id "
                "NOT IN (SELECT match_id FROM grade_raw)").fetchone()["c"]
        report["no_raw"] = {"grade_without_blob": derived,
                            "note": "pochodne sprzed grade_raw (2.09) - "
                                    "luka pokrycia, nie rozjazd"}
    finally:
        adb.DB_PATH = old_path
        src.close()
    return report


def main():
    if len(sys.argv) < 2:
        print("uzycie: python tools/dry_run_rebuild.py /sciezka/KOPII.db")
        return 1
    with tempfile.TemporaryDirectory() as td:
        rep = rebuild_and_compare(sys.argv[1], str(Path(td) / "rebuild.db"))
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    total = (rep["grade"]["mismatch"] + rep["eog"]["mismatch"]
             + rep["player_stat"]["mismatch"] + rep["participants"]["mismatch"])
    print("\nWERDYKT:", "polisa dziala - zero rozjazdow" if total == 0
          else f"{total} rozjazdow - patrz diffs, to sa ciche bugi ekstrakcji")
    return 0 if total == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
