SHELL := /bin/sh
PY := backend/.venv/Scripts/python.exe

.PHONY: help secrets install test lint fmt migrate revision up down logs ui-dev api-dev

help:
	@echo "secrets   Print freshly generated values for .env"
	@echo "install   Create the backend venv and install UI deps"
	@echo "test      Run the backend test suite"
	@echo "lint      Ruff + TypeScript typecheck"
	@echo "migrate   Apply migrations to the configured database"
	@echo "revision  Autogenerate a migration (m=\"message\")"
	@echo "up        Start the full stack"
	@echo "down      Stop the stack"

secrets:
	@python -c "import secrets; \
	print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24)); \
	print('SDWAN_SECRET_KEY=' + secrets.token_urlsafe(48)); \
	print('SDWAN_JWT_SECRET=' + secrets.token_urlsafe(48)); \
	print('SDWAN_BOOTSTRAP_ADMIN_PASSWORD=' + secrets.token_urlsafe(18))"

install:
	cd backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
	cd ui && npm install

test:
	cd backend && .venv/Scripts/python.exe -m pytest -q

lint:
	cd backend && .venv/Scripts/python.exe -m ruff check .
	cd ui && npm run lint

fmt:
	cd backend && .venv/Scripts/python.exe -m ruff check --fix .

migrate:
	cd backend && .venv/Scripts/python.exe -m alembic upgrade head

revision:
	cd backend && .venv/Scripts/python.exe -m alembic revision --autogenerate -m "$(m)"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

api-dev:
	cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000

ui-dev:
	cd ui && npm run dev
