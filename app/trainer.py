"""Trening modelu w osobnym procesie (diagnoza 23.09).

model.train to ~1 min czystego Pythona (wybor l2 + walidacja LOO), a koszt
rosnie z liczba ocen. Uruchamiany w watku serwera trzymal GIL i glodzil
wszystkie inne prace na bazie: na kopii zapytanie 0,03 s trwalo 45 s
w trakcie treningu, a po grze serwer byl dla agenta nieosiagalny przez
kilka minut ("database is locked", champ select z opoznieniem). Osobny
proces ma wlasny interpreter, wiec serwer zostaje responsywny.

Jeden proces roboczy na caly serwer: treningi ida po kolei, a start
procesu (import numpy-free modulow app) placimy raz. spawn zamiast fork:
proces serwera ma watki i petle asyncio, fork kopiowalby ich stan."""
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_POOL = None


def _init_worker():
    # trening moze poczekac, serwer i backup nie - nizszy priorytet CPU
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def train_job(db_path, mode, goal):
    """Wykonywane w procesie roboczym. Sciezka bazy jawnie, bo DB_PATH
    z env nie zna swiata testu ani przywroconej kopii."""
    from app import db, model
    db.DB_PATH = Path(db_path)
    return model.train(mode, True, goal)


def pool():
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker)
    return _POOL


def shutdown():
    """Zamyka proces roboczy; nastepne zlecenie postawi nowy. Wolane przy
    zamknieciu serwera i po padzie procesu (BrokenProcessPool)."""
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None
