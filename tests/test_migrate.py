"""Migracje musza byc idempotentne: druga aplikacja na tej samej bazie
niczego nie psuje. To jest kontrakt, na ktorym stoi kazdy upgrade produkcji."""
from app import db


def test_migrate_twice_is_safe(fresh_db):
    first = db.migrate()   # fixture juz raz odpalila - to jest 2. i 3. przebieg
    second = db.migrate()
    assert first == second and len(first) >= 8


def test_upgrade_functions_included(fresh_db):
    names = db.migrate()
    assert any(n.startswith("upgrade") for n in names)
