"""Config yardimci fonksiyonlari testleri."""

from dataclasses import replace

import pytest
from app.config import _get_bool, _get_float, _get_int, _get_list, settings, validate_production


def test_get_bool(monkeypatch):
    monkeypatch.setenv("X", "true")
    assert _get_bool("X", False) is True
    monkeypatch.setenv("X", "0")
    assert _get_bool("X", True) is False
    monkeypatch.setenv("X", "evet")
    assert _get_bool("X", False) is True
    assert _get_bool("YOK_BOOL", True) is True


def test_get_list(monkeypatch):
    monkeypatch.setenv("L", "a, b ,c")
    assert _get_list("L", []) == ["a", "b", "c"]
    monkeypatch.setenv("L", "")
    assert _get_list("L", ["d"]) == ["d"]
    assert _get_list("YOK_LIST", ["x"]) == ["x"]


def test_get_int(monkeypatch):
    monkeypatch.setenv("N", "42")
    assert _get_int("N", 0) == 42
    monkeypatch.setenv("N", "bozuk")
    assert _get_int("N", 7) == 7


def test_get_float(monkeypatch):
    monkeypatch.setenv("F", "3.14")
    assert _get_float("F", 0.0) == 3.14
    monkeypatch.setenv("F", "x")
    assert _get_float("F", 1.5) == 1.5


def test_validate_production_rejects_auth_disabled():
    cfg = replace(settings, production_mode=True, auth_required=False)
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        validate_production(cfg)


def test_validate_production_passes_with_auth_on():
    validate_production(replace(settings, production_mode=True, auth_required=True))


def test_validate_production_noop_outside_production():
    validate_production(replace(settings, production_mode=False, auth_required=False))
