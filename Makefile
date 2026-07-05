.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test cov run up down logs clean

help: ## Komutlari listele
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Bagimliliklari + pre-commit kur
	pip install -e ".[dev]"
	pre-commit install

lint: ## Ruff ile lint
	ruff check .

format: ## Ruff ile formatla
	ruff format .

typecheck: ## Mypy ile tip kontrolu
	mypy app

test: ## Testleri calistir
	pytest

cov: ## Testleri kapsam raporuyla calistir
	pytest --cov=app --cov-report=term-missing

run: ## Yerel calistir (in-memory, reload)
	uvicorn app.main:app --reload --port 8000

up: ## Docker Compose baslat (build)
	docker compose up -d --build

down: ## Docker Compose durdur
	docker compose down

logs: ## Servis loglari
	docker compose logs -f

clean: ## Gecici dosyalari temizle
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml
