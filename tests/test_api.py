from fastapi.testclient import TestClient

from app.main import app


def test_root():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert r.json()["ana_uc_nokta"] == "/all"


def test_health_ok():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_ready_empty_returns_503():
    # Bos store: trafik almaya hazir degil.
    with TestClient(app) as c:
        r = c.get("/ready")
        assert r.status_code == 503


def test_all_empty_ok():
    with TestClient(app) as c:
        r = c.get("/all")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["quotes"] == []


def test_invalid_symbol_rejected():
    with TestClient(app) as c:
        r = c.get("/quote/TOOLONGSYMBOL")
        assert r.status_code == 400


def test_invalid_sort_rejected():
    with TestClient(app) as c:
        r = c.get("/all", params={"sort": ";rm -rf"})
        assert r.status_code == 400


def test_invalid_period_rejected():
    with TestClient(app) as c:
        r = c.get("/history/THYAO", params={"period": "INVALID"})
        assert r.status_code == 400
