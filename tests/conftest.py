"""Test fikstürleri: arka plan updater'i devre disi birakir (ag cagrisi olmasin)."""

import os

# Settings import oncesi: testler auth/demo icin acik mod.
os.environ.setdefault("AUTH_REQUIRED", "false")
os.environ.setdefault("PRODUCTION_MODE", "false")
os.environ.setdefault("DEMO_ENABLED", "true")

import pytest
from app.updater import updater


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
