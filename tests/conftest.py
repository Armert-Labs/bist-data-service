"""Test fikstürleri: arka plan updater'i devre disi birakir (ag cagrisi olmasin)."""

import os

# Settings import oncesi: testler auth/demo icin acik mod.
os.environ.setdefault("AUTH_REQUIRED", "false")
os.environ.setdefault("PRODUCTION_MODE", "false")
os.environ.setdefault("DEMO_ENABLED", "true")

import pytest
from app.updater import updater


@pytest.fixture
def override_settings():
    """Frozen Settings alanlarini test suresince gecici degistirir."""
    from app.config import settings

    changed: dict[str, object] = {}

    def _set(**kwargs):
        for key, value in kwargs.items():
            if key not in changed:
                changed[key] = getattr(settings, key)
            object.__setattr__(settings, key, value)

    yield _set
    for key, value in changed.items():
        object.__setattr__(settings, key, value)


@pytest.fixture(autouse=True)
def _disable_background(monkeypatch):
    monkeypatch.setattr(updater, "start", lambda: None)
    from app.main import _all_cache

    _all_cache.clear()
    from app.store import MemoryStore, get_store

    store = get_store()
    if isinstance(store, MemoryStore):
        store._quotes = {}
        store._history = {}
        store._last_update = None
        store._negative = {}
        store._history_cache = {}


@pytest.fixture(autouse=True)
def _reset_guard_cooldown_gauge():
    """MEDIUM-1: `bist_provider_guard_cooldown` gauge process-genelinde
    paylasilir (prometheus_client) -- bir testin biraktigi provider=X, deger=1
    durumu, testin kendisi sifirlamadiysa SONRAKI testin (farkli/izole bir
    Aggregator ile calisan) assertion'ina SIZAR. Ozellikle testler TEK
    BASINA (`-k` ile) calistirildiginda -- bu durumda "onceki test sifirladi"
    tesadufu de kaybolur -- yalitimsizlik aciga cikar. Her testten ONCE tum
    label kombinasyonlarini temizler (deger 0'a doner)."""
    from app import metrics

    metrics.PROVIDER_GUARD_COOLDOWN._metrics.clear()
    yield
    metrics.PROVIDER_GUARD_COOLDOWN._metrics.clear()
