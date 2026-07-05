"""CircuitBreaker testleri."""

import time

from app.providers.base import CircuitBreaker


def test_starts_closed():
    cb = CircuitBreaker("x", fail_threshold=3, reset_timeout=60)
    assert cb.state == "closed"
    assert cb.allow() is True
    assert cb.healthy is True


def test_opens_after_threshold():
    cb = CircuitBreaker("x", fail_threshold=3, reset_timeout=60)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False
    assert cb.healthy is False


def test_success_resets_failures():
    cb = CircuitBreaker("x", fail_threshold=2, reset_timeout=60)
    cb.record_failure()
    cb.record_success()
    cb.record_failure()  # sayac sifirlandigi icin henuz acilmaz
    assert cb.state == "closed"


def test_half_open_after_timeout_then_closes():
    cb = CircuitBreaker("x", fail_threshold=1, reset_timeout=0.01)
    cb.record_failure()
    assert cb.state == "open"
    time.sleep(0.02)
    assert cb.state == "half_open"
    assert cb.allow() is True
    cb.record_success()
    assert cb.state == "closed"
