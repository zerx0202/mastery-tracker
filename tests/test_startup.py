"""Start aplikacji z lifespanem - smoke GET-y go omijaja, a to wlasnie
w lifespanie siedzi migrate() i taski tla. Dzisiejszy TypeError z dekoratora
przeszedlby przez CI bez tego testu."""
from fastapi.testclient import TestClient

from app.main import app


def test_app_starts_with_lifespan(fresh_db):
    with TestClient(app) as client:   # context manager = pelny lifespan
        r = client.get("/api/health")
        assert r.status_code == 200
