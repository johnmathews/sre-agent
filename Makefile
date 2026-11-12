.PHONY: install dev lint format typecheck test test-e2e check serve ingest chat hooks eval report advisor smoke docker-build docker-up docker-down web-install web-dev web-lint web-check web-image

install:
	uv sync

dev:
	uv sync --group dev

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

typecheck:
	uv run mypy src/

test:
	uv run coverage run -m pytest -rf
	uv run coverage report

test-e2e:
	uv run pytest --run-e2e

check: lint typecheck test

serve:
	uv run uvicorn src.api.main:app --reload --port 8000

ingest:
	uv run python -m scripts.ingest_runbooks

chat:
	uv run python -m src.cli

eval:
	uv run python -m scripts.run_eval $(ARGS)

report:
	uv run python -m scripts.run_report

advisor:
	uv run python -m src.upgrades --dry-run

hooks:
	bash scripts/install-hooks.sh

# Build the image and smoke-test it (the same check CI runs before pushing).
smoke:
	docker build -t sre-agent:smoke .
	bash scripts/smoke_image.sh sre-agent:smoke

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

# --- Web UI (web/, npm) ---

web-install:
	cd web && npm ci && npx playwright install chromium webkit

# Vite on :5173, proxying /api/* to `make serve` on :8000.
web-dev:
	cd web && npm run dev

# ESLint on web/ (Vue essential + typescript-eslint recommended; no warnings allowed).
web-lint:
	cd web && npm run lint

# What CI's web-test job runs: typecheck, lint, then Playwright against a mocked backend.
web-check:
	cd web && npm run typecheck && npm run lint && npx playwright test

# Build the production UI image locally (CI's web-build job builds and pushes it).
web-image:
	docker build -t sre-webapp:local web
