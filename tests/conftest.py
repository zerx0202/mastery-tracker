"""Wspolna baza testow: kazdy test dostaje swieza baze SQLite w pliku
tymczasowym. Schematy zbieramy ze wszystkich stalych *SCHEMA* w db.py,
zeby test nie rozjechal sie z produkcja przy dodaniu tabeli."""
import os

import pytest

# Atrapy srodowiska - testy nie moga wymagac prawdziwego klucza ani konta.
# Musza byc ustawione PRZED importem app.*, bo main.py czyta env przy imporcie.
os.environ.setdefault("DB_PATH", "/tmp/mastery-test-import.db")
os.environ.setdefault("RIOT_API_KEY", "RGAPI-test-key-not-real")
os.environ.setdefault("RIOT_ID", "Test#EUW")
os.environ.setdefault("MY_RIOT_NAME", "Test")
os.environ.setdefault("MY_RIOT_TAG", "EUW")
os.environ.setdefault("API_TOKEN", "")

from app import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    # Ta sama sciezka inicjalizacji co w aplikacji - wszystkie funkcje init*
    # w kolejnosci definicji. Dzieki temu baza testowa = baza produkcyjna
    # z definicji, razem z ALTER-ami dokladajacymi kolumny.
    db.migrate()
    return path


def insert_row(con, table, **cols):
    """INSERT z automatycznym wypelnieniem kolumn NOT NULL bez defaultu.
    Test podaje tylko to, co go obchodzi."""
    info = con.execute(f"PRAGMA table_info({table})").fetchall()
    # jedyny PK, ktorego nie wolno wypelniac, to pojedynczy INTEGER PRIMARY KEY
    # (alias rowid) - kazdy inny, w tym skladowe klucza zlozonego, trzeba podac
    pk_cols = [r for r in info if r[5]]
    auto_pk = (len(pk_cols) == 1 and "INT" in (pk_cols[0][2] or "").upper())
    row = {}
    for _cid, name, ctype, notnull, default, pk in info:
        if name in cols:
            row[name] = cols[name]
        elif pk and auto_pk:
            continue
        elif (notnull or pk) and default is None:
            row[name] = 0 if any(t in (ctype or "").upper()
                                 for t in ("INT", "REAL", "NUM")) else ""
    keys = ", ".join(row)
    marks = ", ".join(":" + k for k in row)
    con.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", row)
