# Katkı Rehberi

Katkılarınız için teşekkürler! Bu proje, BIST için gecikmeli canlı fiyat
mikroservisidir.

## Geliştirme Ortamı

```bash
git clone https://github.com/Armert-Labs/bist-data-service.git
cd bist-data-service

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Yaygın Komutlar (Makefile)

```bash
make install     # bagimliliklar + pre-commit
make lint        # ruff kontrol
make format      # ruff format
make typecheck   # mypy
make test        # pytest + coverage
make run         # yerel calistir (in-memory)
make up / down   # docker compose
```

## Kod Standartları

- **Ruff** (lint + format) ve **mypy** (tip kontrolü) CI'da zorunludur.
- Yeni özellik/hata düzeltmesi için **test** ekleyin (`tests/`).
- Genel/harici kaynak entegrasyonları circuit breaker + sanity-check ile korunmalıdır.
- Sırlar (anahtar, token) koda veya loglara asla yazılmaz.

## Pull Request Süreci

1. `main`'den bir dal açın: `git checkout -b feat/kisa-aciklama`
2. Değişikliğinizi yapın + test ekleyin.
3. `make lint typecheck test` yerelde geçmeli.
4. Açıklayıcı bir PR açın (şablon otomatik gelir).
5. CI yeşil olmalı; en az bir onay sonrası merge edilir.

## Commit Mesajları

Kısa, açıklayıcı ve emir kipinde. Örn:
`feat: is yatirim gap-fill modu`, `fix: sse pubsub kapanis sizintisi`.
