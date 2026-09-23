"""Zaden async def w app/main.py nie czyta ani nie pisze bazy (ani nie liczy
modelu) bezposrednio na petli zdarzen (diagnoza 23.09): przy zapisie po
grze SQLite czeka do 10 s (busy_timeout), a jeden taki odczyt zamrazal caly
serwer - razem z POST-ami agenta z champ selecta. Dozwolone: argument
asyncio.to_thread, zagniezdzone def (puszczane w watku), lambdy przekazywane
dalej, zwykle def (FastAPI wykonuje je w puli watkow)."""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

# migrate() przy starcie jest celowo synchroniczny (nic jeszcze nie obsluguje
# ruchu); _find_local_player to czyste parsowanie slownika, bez bazy
ALLOWED = {("lifespan", "db.migrate"), ("push_eog", "db._find_local_player")}


def _db_touching_sync_functions(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for c in ast.walk(node):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and isinstance(c.func.value, ast.Name)
                        and c.func.value.id in ("db", "model")):
                    out.add(node.name)
                    break
    return out


class _Finder(ast.NodeVisitor):
    def __init__(self, touching):
        self.touching = touching
        self.hits = []
        self.depth_thread = 0

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "to_thread":
            self.depth_thread += 1
            self.generic_visit(node)
            self.depth_thread -= 1
            return
        if not self.depth_thread:
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id in ("db", "model")):
                self.hits.append(f"{f.value.id}.{f.attr}")
            elif isinstance(f, ast.Name) and f.id in self.touching:
                self.hits.append(f.id)
        self.generic_visit(node)

    def visit_Lambda(self, node):
        return

    def visit_FunctionDef(self, node):
        return


def test_async_handlers_keep_database_off_the_event_loop():
    tree = ast.parse(SRC)
    touching = _db_touching_sync_functions(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            finder = _Finder(touching)
            for stmt in node.body:
                finder.visit(stmt)
            bad += [f"{node.name}: {h}" for h in finder.hits
                    if (node.name, h) not in ALLOWED]
    assert bad == [], "odczyt/zapis bazy na petli zdarzen: " + ", ".join(bad)
