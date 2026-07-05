# Değişiklik Günlüğü

Bu projenin tüm önemli değişiklikleri bu dosyada belgelenir.

Biçim [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) temellidir ve
proje [Semantic Versioning](https://semver.org/lang/tr/) kullanır.

## [Yayınlanmamış]

### Eklendi
- Modern paketleme (`pyproject.toml`), ruff + mypy + pre-commit
- MIT lisansı, SECURITY / CONTRIBUTING / CODE_OF_CONDUCT
- GitHub şablonları (issue/PR), Dependabot, CODEOWNERS
- Genişletilmiş CI (lint + tip kontrolü + kapsam + matris + Docker imajı)
- `Makefile`, Prometheus + Grafana örnek yapılandırmaları

## [0.1.0] - 2026-07-05

### Eklendi
- FastAPI + Redis (önbellek + pub/sub) tabanlı mikroservis; Docker Compose (api + updater + redis)
- Çoklu kaynak: Yahoo (yfinance + v8 chart) + İş Yatırım fallback + circuit breaker
- REST uç noktaları: `/all`, `/quote`, `/quotes`, `/history`, `/intraday`, `/symbols`
- SSE canlı akış (`/stream`) — Redis pub/sub fan-out
- Fiyat doğrulama (`/validate`) — çapraz kaynak karşılaştırma + sapma metriği
- Prometheus `/metrics`, JSON loglama, `/health` + `/ready` probe'ları
- API key kimlik doğrulama (çoklu key, timing-safe, SHA-256), rate limit, sanity-check, staleness
- pytest test paketi + GitHub Actions CI

[Yayınlanmamış]: https://github.com/Armert-Labs/bist-data-service/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Armert-Labs/bist-data-service/releases/tag/v0.1.0
