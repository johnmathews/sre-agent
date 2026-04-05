.PHONY: install dev lint format typecheck test test-e2e check serve ingest chat hooks eval report docker-build docker-up docker-down

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
	uv run pytest

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

hooks:
	bash scripts/install-hooks.sh

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
