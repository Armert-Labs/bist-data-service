"""Test fikstürleri: arka plan updater'i devre disi birakir (ag cagrisi olmasin)."""

import pytest
from app.updater import updater


@pytest.fixture(autouse=True)
def _disable_background(monkeypatch):
    # Lifespan sirasinda gercek Yahoo cekimi baslatilmasin.
    monkeypatch.setattr(updater, "start", lambda: None)
    # /all micro-cache ve negatif onbellegi temizle (testler arasi izolasyon).
    from app.main import _all_cache, _negative_cache

    _all_cache.clear()
    _negative_cache.clear()
    # In-memory store'u sifirla (testler arasi izolasyon).
    from app.store import MemoryStore, get_store

    store = get_store()
    if isinstance(store, MemoryStore):
        store._quotes = {}
        store._history = {}
        store._last_update = None
