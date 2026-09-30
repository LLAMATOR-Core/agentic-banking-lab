.PHONY: up up-core down logs ps test lint smoke reset

up:
	docker compose up --build -d

up-core:
	docker compose up --build -d postgres mongo keycloak abs-api memory-orchestrator abs-mcp abs-mcp-vulnerable duckduckgo-mcp booking-mcp expedia-mcp travel-data-mcp travel-agent banking-agent ui

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

test:
	python -m pytest -q

lint:
	python -m ruff check services tests scripts

smoke:
	python scripts/smoke_test.py

# Destructive: removes all local stand data after an explicit command by the operator.
reset:
	docker compose down -v
