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


def test_all_market_state_reflects_current(monkeypatch):
    """Kapali seansta cache'teki eski OPEN damgasi istemciye sizmamali."""
    _seed_store()
    monkeypatch.setattr("app.main.market_state", lambda: "CLOSED")
    with TestClient(app) as c:
        body = c.get("/all").json()
        assert body["market"] == "CLOSED"
        assert all(q["market_state"] == "CLOSED" for q in body["quotes"])


def test_quotes_market_state_reflects_current(monkeypatch):
    _seed_store()
    monkeypatch.setattr("app.main.market_state", lambda: "CLOSED")
    with TestClient(app) as c:
        body = c.get("/quotes", params={"symbols": "THYAO"}).json()
        assert body["quotes"]["THYAO"]["market_state"] == "CLOSED"


def test_all_cache_respects_market_flip(monkeypatch):
    """Micro-cache isabeti kapanis anindaki state gecisini gizlememeli."""
    _seed_store()
    with TestClient(app) as c:
        monkeypatch.setattr("app.main.market_state", lambda: "OPEN")
        assert c.get("/all").json()["market"] == "OPEN"
        monkeypatch.setattr("app.main.market_state", lambda: "CLOSED")
        assert c.get("/all").json()["market"] == "CLOSED"


def test_sse_payload_applies_live_state():
    from app.main import _sse_quotes_payload
    from app.models import Quote

    q = Quote(symbol="THYAO", price=1.0, market_state="OPEN")
    out = _sse_quotes_payload([q], None, "CLOSED")
    assert out["THYAO"]["market_state"] == "CLOSED"
    out = _sse_quotes_payload([q], frozenset({"GARAN"}), "CLOSED")
    assert out == {}


def test_ready_reports_freshness_fields():
    _seed_store()
    with TestClient(app) as c:
        body = c.get("/ready").json()
        assert body["fresh_pct"] == 100.0
        assert body["last_update_age_seconds"] is not None
        assert body["last_update_age_seconds"] < 5


def test_ready_returns_structured_503_when_store_down(monkeypatch):
    """Redis coktugunde /ready 500 degil, yapisal 503 govdesi dondurmeli."""

    async def dead_ping():
        return False

    async def boom():
        raise ConnectionError("redis dead")

    monkeypatch.setattr("app.main.store.ping", dead_ping)
    monkeypatch.setattr("app.main.store.is_stale", boom)
    with TestClient(app) as c:
        r = c.get("/ready")
        assert r.status_code == 503
        assert r.json()["detail"]["store_ok"] is False


def test_quotes_reports_missing_symbols(monkeypatch):
    """Istemci 'sembol yok' ile 'veri gelmedi'yi ayirt edebilmeli."""
    _seed_store()

    async def fake_commit(store, symbols, **kw):
        return {}

    monkeypatch.setattr("app.main.fetch_and_commit", fake_commit)
    with TestClient(app) as c:
        body = c.get("/quotes", params={"symbols": "THYAO,ZZZZ"}).json()
        assert body["count"] == 1
        assert body["missing"] == ["ZZZZ"]


def test_all_etag_304():
    _seed_store()
    with TestClient(app) as c:
        r1 = c.get("/all")
        etag = r1.headers.get("etag")
        assert etag
        r2 = c.get("/all", headers={"If-None-Match": etag})
        assert r2.status_code == 304
        assert r2.content == b""


def test_all_etag_stable_despite_data_age_drift(override_settings, monkeypatch):
    """LOW-a: data_age_seconds okuma aninda surekli degistigi icin ETag'i
    dogrudan govde hash'inden turetmek 304 yolunu oldururdu (fiyat AYNI kalsa
    bile her yeniden hesaplamada ETag degisirdi). ETag yalnizca 'gercek' veriden
    (yas alanlari haric) turetilmeli."""
    from datetime import UTC, timedelta
    from datetime import datetime as real_datetime

    override_settings(all_cache_ttl=0.0)  # cache'i devre disi birak, HER istek yeniden hesaplasin
    _seed_store()

    calls = {"n": 0}
    base = real_datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            calls["n"] += 1
            return base + timedelta(seconds=calls["n"] * 60)

    monkeypatch.setattr("app.main.datetime", FakeDateTime)
    with TestClient(app) as c:
        r1 = c.get("/all")
        etag1 = r1.headers["etag"]
        age1 = r1.json()["quotes"][0]["data_age_seconds"]
        r2 = c.get("/all")
        etag2 = r2.headers["etag"]
        age2 = r2.json()["quotes"][0]["data_age_seconds"]
    assert age1 != age2  # "saat" gercekten ilerledi, yeniden hesaplandi
    assert etag1 == etag2  # ama ETag SABIT kaldi (fix)


def test_all_gzip_encoding():
    from datetime import UTC, datetime

    from app.models import Quote
    from app.store import get_store

    store = get_store()
    now = datetime.now(UTC)
    store._quotes = {
        f"SYM{i:02d}": Quote(symbol=f"SYM{i:02d}", price=float(i + 1), updated_at=now)
        for i in range(20)
    }
    store._last_update = now
    with TestClient(app) as c:
        r = c.get("/all", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip"


def test_rate_limit_response_has_retry_after():
    from app.main import _rate_limit_response

    resp = _rate_limit_response()
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"  # default 120/minute -> 60 sn pencere


def test_validate_reports_compared_flag(monkeypatch):
    """Hicbir referansa erisilemezse 'tutarsiz' degil 'karsilastirilamadi' denmeli."""
    _seed_store()
    monkeypatch.setattr("app.main.aggregator.get_provider", lambda name: None)
    with TestClient(app) as c:
        body = c.get("/validate", params={"symbols": "THYAO"}).json()
        assert body["compared"] is False


def test_quotes_symbol_limit_boundary_passes(override_settings):
    from datetime import UTC, datetime

    from app.models import Quote
    from app.store import get_store

    override_settings(max_symbols_per_request=5)
    store = get_store()
    now = datetime.now(UTC)
    syms = [f"S{i:03d}" for i in range(5)]
    store._quotes = {s: Quote(symbol=s, price=1.0, updated_at=now) for s in syms}
    store._last_update = now
    with TestClient(app) as c:
        r = c.get("/quotes", params={"symbols": ",".join(syms)})
        assert r.status_code == 200
        assert r.json()["count"] == 5


def test_quotes_symbol_limit_exceeded_rejected(override_settings):
    override_settings(max_symbols_per_request=5)
    with TestClient(app) as c:
        syms = ",".join(f"S{i:03d}" for i in range(6))
        r = c.get("/quotes", params={"symbols": syms})
        assert r.status_code == 400
        assert "5" in r.json()["detail"]


def test_validate_symbol_limit_exceeded_rejected(override_settings):
    override_settings(max_symbols_per_request=5)
    with TestClient(app) as c:
        syms = ",".join(f"S{i:03d}" for i in range(6))
        r = c.get("/validate", params={"symbols": syms})
        assert r.status_code == 400
        assert "5" in r.json()["detail"]


def test_quote_reports_stale_when_open_and_old(monkeypatch, override_settings):
    from datetime import UTC, datetime, timedelta

    from app.models import Quote
    from app.store import get_store

    override_settings(staleness_seconds=300)
    monkeypatch.setattr("app.main.market_state", lambda: "OPEN")
    store = get_store()
    old = datetime.now(UTC) - timedelta(seconds=1000)
    store._quotes = {"THYAO": Quote(symbol="THYAO", price=100.0, updated_at=old)}
    store._last_update = old
    with TestClient(app) as c:
        body = c.get("/quote/THYAO").json()
        assert body["stale"] is True
        assert body["data_age_seconds"] > 900


def test_quote_not_stale_when_market_closed_even_if_old(monkeypatch, override_settings):
    from datetime import UTC, datetime, timedelta

    from app.models import Quote
    from app.store import get_store

    override_settings(staleness_seconds=300)
    monkeypatch.setattr("app.main.market_state", lambda: "CLOSED")
    store = get_store()
    old = datetime.now(UTC) - timedelta(seconds=10000)
    store._quotes = {"THYAO": Quote(symbol="THYAO", price=100.0, updated_at=old)}
    store._last_update = old
    with TestClient(app) as c:
        body = c.get("/quote/THYAO").json()
        # Kapali seansta son kapanis mesru veridir; ne kadar eski olursa olsun
        # bayat SAYILMAZ (README felsefesiyle tutarli).
        assert body["stale"] is False


def test_quote_fresh_when_recently_updated(monkeypatch):
    from datetime import UTC, datetime

    from app.models import Quote
    from app.store import get_store

    monkeypatch.setattr("app.main.market_state", lambda: "OPEN")
    store = get_store()
    now = datetime.now(UTC)
    store._quotes = {"THYAO": Quote(symbol="THYAO", price=100.0, updated_at=now)}
    store._last_update = now
    with TestClient(app) as c:
        body = c.get("/quote/THYAO").json()
        assert body["stale"] is False
        assert body["data_age_seconds"] < 5


def test_quote_data_age_never_negative_for_future_exchange_time(monkeypatch):
    # MEDIUM-2: exchange_time (orn. isyatirim'in kapanis-zamani tahmini) saat
    # kaymasi/klemp bosluğu nedeniyle "simdi"den ileride kalirsa data_age_seconds
    # negatif GORUNMEMELI (klemplenir).
    from datetime import UTC, datetime, timedelta

    from app.models import Quote
    from app.store import get_store

    monkeypatch.setattr("app.main.market_state", lambda: "OPEN")
    store = get_store()
    future = datetime.now(UTC) + timedelta(seconds=120)
    store._quotes = {
        "THYAO": Quote(symbol="THYAO", price=100.0, exchange_time=future, updated_at=future)
    }
    store._last_update = future
    with TestClient(app) as c:
        body = c.get("/quote/THYAO").json()
        assert body["data_age_seconds"] >= 0


def test_all_quotes_report_data_age_seconds():
    _seed_store()
    with TestClient(app) as c:
        body = c.get("/all").json()
        for q in body["quotes"]:
            assert q["data_age_seconds"] is not None
            assert q["data_age_seconds"] < 5
            assert q["stale"] is False


def test_validate_threshold_from_settings(monkeypatch, override_settings):
    from app.models import Quote

    override_settings(cross_validate_max_pct=5.0)
    _seed_store()

    class FakeRef:
        name = "yahoo_chart"

        async def fetch_quotes(self, symbols):
            return {s: Quote(symbol=s, price=334.0 * 1.03) for s in symbols}  # %3 sapma

    monkeypatch.setattr("app.main.aggregator.get_provider", lambda name: FakeRef())
    with TestClient(app) as c:
        body = c.get("/validate", params={"symbols": "THYAO"}).json()
        assert body["consistent"] is True  # %3 < %5 (esik artik ayardan)
        assert body["threshold_pct"] == 5.0
