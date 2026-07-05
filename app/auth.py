"""API anahtari dogrulama (coklu key, timing-safe, hash destegi).

Iki kaynak desteklenir:
  API_KEYS          : duz metin anahtarlar.   "key1:mobil,key2:web" veya "key1,key2"
  API_KEYS_SHA256   : SHA-256 hash'li anahtarlar (uretimde plaintext'ten kacinmak icin).
                      "<hex_hash>:label,..."
  API_KEY           : (geriye uyum) tek duz metin anahtar.

Guvenlik notlari:
- Karsilastirma hmac.compare_digest ile SABIT ZAMANLI yapilir (timing attack kalkani).
- Hash modunda anahtarin kendisi sunucuda tutulmaz; yalnizca SHA-256'si.
- verify() eslesen anahtarin etiketini (label) doner -> denetim/loglama icin.

Anahtar uretmek icin:
  python -c "import secrets; print(secrets.token_urlsafe(32))"
Hash almak icin:
  python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <KEY>
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)


class ApiKeyRegistry:
    def __init__(self) -> None:
        # (secret, label, is_hash)
        self._entries: list[tuple[str, str, bool]] = []
        self._load()

    @staticmethod
    def _split_label(item: str, default: str) -> tuple[str, str]:
        secret, _, label = item.partition(":")
        return secret.strip(), (label.strip() or default)

    def _load(self) -> None:
        # Geriye uyum: tekil API_KEY
        if settings.api_key:
            self._entries.append((settings.api_key, "default", False))

        for item in settings.api_keys:
            secret, label = self._split_label(item, "unnamed")
            if secret:
                self._entries.append((secret, label, False))

        for item in settings.api_keys_sha256:
            secret, label = self._split_label(item, "unnamed")
            if secret:
                self._entries.append((secret.lower(), label, True))

        if self._entries:
            labels = ", ".join(sorted({e[1] for e in self._entries}))
            logger.info("API key registry: %d anahtar yuklendi (%s).", len(self._entries), labels)

    @property
    def enabled(self) -> bool:
        return len(self._entries) > 0

    def verify(self, presented: Optional[str]) -> Optional[str]:
        """Gecerliyse etiketi, degilse None doner. Sabit zamanli."""
        if not presented:
            return None
        presented_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        matched: Optional[str] = None
        # Erken cikmadan tum girdileri dolas (timing sizintisini azalt).
        for secret, label, is_hash in self._entries:
            candidate = presented_hash if is_hash else presented
            if hmac.compare_digest(candidate, secret):
                matched = label
        return matched


registry = ApiKeyRegistry()
