"""Statyki frontu (app.js, style.css, index.html) maja byc rewalidowane przy
kazdym otwarciu: bez Cache-Control przegladarka zostawala na starym app.js
po deployu, a w oknie PWA nie ma nawet paska adresu do twardego refreshu.
no-cache + ETag z StaticFiles = 304 przy braku zmian, swieza wersja od razu
po niej. API bez tego naglowka (odpowiedzi i tak nie sa cache'owane)."""
from fastapi.testclient import TestClient

from app.main import app


def test_static_files_are_always_revalidated(fresh_db):
    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/", "/app.js", "/style.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    etag = client.get("/app.js").headers["etag"]
    assert client.get("/app.js", headers={"If-None-Match": etag}).status_code == 304
    assert "cache-control" not in client.get("/api/sentinel").headers
