"""Test fikstürleri: arka plan updater'i devre disi birakir (ag cagrisi olmasin)."""

import pytest

from app.updater import updater


@pytest.fixture(autouse=True)
def _disable_background(monkeypatch):
    # Lifespan sirasinda gercek Yahoo cekimi baslatilmasin.
    monkeypatch.setattr(updater, "start", lambda: None)
    # /all micro-cache'i temizle (testler arasi izolasyon).
    from app.main import _all_cache
    _all_cache.clear()
