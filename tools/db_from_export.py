#!/usr/bin/env python3
"""
Kopia bazy do analiz z JSON-a /api/export (partia L, 4.09).

Schemat przez db.migrate() - ta sama sciezka co produkcja i fixture fresh_db
- potem INSERT OR REPLACE kazdej tabeli obecnej w schemacie. Eksport nie ma
blobow (kolumny payload), wiec tabele z NOT NULL payload (eog_raw, grade_raw,
match_timeline) sa POMIJANE i raportowane - to kopia pod oceny, mecze,
statystyki, snapshoty i predykcje, nie pod tools/dry_run_rebuild.py (ten
potrzebuje .backup z blobami).

Uzycie: python tools/db_from_export.py export.json /sciezka/NOWEJ_kopii.db
Nic nie nadpisuje: istniejaca sciezka = odmowa. Kopia nigdy nie jest zywa
baza; narzedzia analityczne dostaja ja przez DB_PATH (timing_analysis,
fatigue_analysis).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build(export_path, db_path):
    from app import db
    db.DB_PATH = Path(db_path)
    db.migrate()
    data = json.load(open(export_path, encoding="utf-8"))
    counts, skipped = {}, {}
    with db.connect() as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t, rows in data.items():
            if t not in tables or not rows:
                continue
            info = con.execute(f"PRAGMA table_info({t})").fetchall()
            have = {r[1] for r in info}
            required = {r[1] for r in info if r[3] and r[4] is None and not r[5]}
            missing = required - set(rows[0].keys())
            if missing:
                skipped[t] = sorted(missing)
                continue
            n = 0
            for r in rows:
                cols = [c for c in r if c in have]
                con.execute(
                    f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])
                n += 1
            counts[t] = n
    return counts, skipped


def main():
    if len(sys.argv) < 3:
        print("uzycie: python tools/db_from_export.py export.json /sciezka/kopii.db")
        return 1
    if Path(sys.argv[2]).exists():
        print("kopia juz istnieje - podaj nowa sciezke (narzedzie nic nie nadpisuje)")
        return 1
    counts, skipped = build(sys.argv[1], sys.argv[2])
    print(json.dumps({"wstawione": counts, "pominiete_bez_blobow": skipped},
                     indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
