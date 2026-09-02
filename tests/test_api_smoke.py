"""Smoke: kazdy publiczny GET ma odpowiadac bez 500 na swiezej, pustej bazie.
Dokladnie ten test zlapalby KeyError w read_lobby, ktory na produkcji
polozyl champ select na wiele godzin."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

GETS = ["/api/health", "/api/lobby", "/api/targets", "/api/split/progress",
        "/api/grades/history", "/api/model/readiness", "/api/norms",
        "/api/system/health", "/api/limits", "/api/balance"]


@pytest.mark.parametrize("path", GETS)
def test_get_never_500(fresh_db, path):
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(path)
    assert r.status_code < 500, f"{path} -> {r.status_code}: {r.text[:200]}"
