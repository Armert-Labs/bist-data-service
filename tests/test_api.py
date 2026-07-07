from app.main import app
from fastapi.testclient import TestClient


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


def _seed_store():
    from datetime import UTC, datetime

    from app.models import Quote
    from app.store import get_store

    store = get_store()
    now = datetime.now(UTC)
    # updated_at zorunlu: yoksa seans acikken is_stale=True olur ve testler
    # gunun saatine gore gecer/kalir (saate bagimli test tuzagi).
    store._quotes = {
        "THYAO": Quote(
            symbol="THYAO",
            price=334.0,
            change=0.75,
            change_percent=0.22,
            volume=1000,
            updated_at=now,
        ),
        "GARAN": Quote(
            symbol="GARAN",
            price=133.5,
            change=-5.1,
            change_percent=-3.68,
            volume=2000,
            updated_at=now,
        ),
    }
    store._last_update = now


def test_all_with_data():
    _seed_store()
    with TestClient(app) as c:
        body = c.get("/all").json()
        assert body["count"] == 2
        assert {q["symbol"] for q in body["quotes"]} == {"THYAO", "GARAN"}
        assert body["is_stale"] is False


def test_all_sorted_by_change_percent_desc():
    _seed_store()
    with TestClient(app) as c:
        body = c.get("/all", params={"sort": "change_percent", "order": "desc"}).json()
        assert body["quotes"][0]["symbol"] == "THYAO"  # +0.22 > -3.68


def test_quotes_selected_symbol():
    _seed_store()
    with TestClient(app) as c:
        body = c.get("/quotes", params={"symbols": "THYAO"}).json()
        assert body["count"] == 1
        assert "THYAO" in body["quotes"]


def test_ready_ok_with_fresh_data():
    _seed_store()
    with TestClient(app) as c:
        r = c.get("/ready")
        assert r.status_code == 200
        assert r.json()["ready"] is True


def test_symbols_list():
    with TestClient(app) as c:
        body = c.get("/symbols").json()
        assert body["count"] > 100
        assert "THYAO" in body["symbols"]


def test_intraday_empty_ok():
    with TestClient(app) as c:
        body = c.get("/intraday/THYAO").json()
        assert body["symbol"] == "THYAO"
        assert body["count"] == 0


def test_metrics_accessible_without_key_in_dev():
    # Test ortaminda anahtar yok + AUTH_REQUIRED yok -> /metrics acik
    with TestClient(app) as c:
        r = c.get("/metrics")
        assert r.status_code == 200


def test_demo_page():
    with TestClient(app) as c:
        r = c.get("/demo")
        assert r.status_code == 200
        assert "BIST" in r.text


def test_history_endpoint(monkeypatch):
    from datetime import UTC, datetime

    from app.models import HistoryBar, HistoryResponse

    async def fake_hist(symbol, period, interval):
        return HistoryResponse(
            symbol=symbol,
            period=period,
            interval=interval,
            bars=[HistoryBar(time=datetime(2026, 1, 1, tzinfo=UTC), close=10.0)],
        )

    monkeypatch.setattr("app.main.aggregator.fetch_history", fake_hist)
    with TestClient(app) as c:
        body = c.get("/history/THYAO", params={"period": "1mo", "interval": "1d"}).json()
        assert len(body["bars"]) == 1


def test_quote_on_demand_fetch(monkeypatch):
    from app.models import Quote

    async def fake_commit(store, symbols, **kwargs):
        quotes = {s: Quote(symbol=s, price=42.0) for s in symbols}
        for q in quotes.values():
            await store.set_quote(q.symbol, q)
        return quotes

    monkeypatch.setattr("app.main.fetch_and_commit", fake_commit)
    with TestClient(app) as c:
        body = c.get("/quote/THYAO").json()
        assert body["price"] == 42.0


def test_quote_negative_cache_prevents_upstream_hammering(monkeypatch):
    """Bulunamayan sembole tekrarli istekler upstream'e YALNIZCA BIR KEZ gitmeli."""
    calls = {"n": 0}

    async def fake_commit(store, symbols, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr("app.main.fetch_and_commit", fake_commit)
    with TestClient(app) as c:
        for _ in range(5):
            assert c.get("/quote/ZZZZ").status_code == 404
    assert calls["n"] == 1  # 4 istek negatif onbellekten dondu


def test_all_bytes_cache_hit_identical(monkeypatch):
    """/all cache isabetinde ayni govde donmeli (serialize-once)."""
    _seed_store()
    with TestClient(app) as c:
        r1 = c.get("/all")
        r2 = c.get("/all")
        assert r1.status_code == r2.status_code == 200
        assert r1.content == r2.content
        assert r1.headers["content-type"].startswith("application/json")


def test_validate_endpoint(monkeypatch):
    from app.models import Quote

    _seed_store()  # primary: THYAO=334, GARAN=133.5

    class FakeRef:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=334.0 if s == "THYAO" else 133.5) for s in symbols}

    monkeypatch.setattr("app.main.aggregator.get_provider", lambda name: FakeRef())
    with TestClient(app) as c:
        body = c.get("/validate", params={"symbols": "THYAO,GARAN"}).json()
        assert body["consistent"] is True
        assert body["max_deviation_pct"] == 0.0
