"""API key dogrulama testleri."""

import hashlib

from app.auth import ApiKeyRegistry, registry
from app.main import app
from fastapi.testclient import TestClient


def _registry_with(entries):
    reg = ApiKeyRegistry.__new__(ApiKeyRegistry)
    reg._entries = entries
    return reg


def test_verify_plaintext():
    reg = _registry_with([("secret123", "mobil", False)])
    assert reg.enabled is True
    assert reg.verify("secret123") == "mobil"
    assert reg.verify("yanlis") is None
    assert reg.verify(None) is None
    assert reg.verify("") is None


def test_verify_hashed():
    h = hashlib.sha256(b"gizli-anahtar").hexdigest()
    reg = _registry_with([(h, "web", True)])
    assert reg.verify("gizli-anahtar") == "web"
    assert reg.verify("baska") is None


def test_verify_multiple_keys():
    reg = _registry_with([("k1", "a", False), ("k2", "b", False)])
    assert reg.verify("k1") == "a"
    assert reg.verify("k2") == "b"
    assert reg.verify("k3") is None


def test_disabled_registry_allows_open_access():
    reg = _registry_with([])
    assert reg.enabled is False
    assert reg.verify("anything") is None


def test_endpoint_enforces_key_when_enabled(monkeypatch):
    monkeypatch.setattr(registry, "_entries", [("testkey", "test", False)])
    with TestClient(app) as c:
        assert c.get("/all").status_code == 401  # eksik
        assert c.get("/all", headers={"X-API-Key": "yanlis"}).status_code == 401  # gecersiz
        assert c.get("/all", headers={"X-API-Key": "testkey"}).status_code == 200  # gecerli


def test_public_endpoints_no_key_needed(monkeypatch):
    monkeypatch.setattr(registry, "_entries", [("testkey", "test", False)])
    with TestClient(app) as c:
        # health/ready auth istemez (probe'lar icin)
        assert c.get("/health").status_code == 200
        assert c.get("/ready").status_code in (200, 503)
