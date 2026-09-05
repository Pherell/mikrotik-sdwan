SHELL := /bin/sh

# Interpreter used to *create* the venv.
#
# Two traps this avoids. Debian and Ubuntu ship python3 with no `python` alias,
# so the bare name cannot be assumed. And Windows puts a `python3` shim on PATH
# that is not an interpreter at all -- it prints an advert for the Microsoft
# Store and exits non-zero. So each candidate is *executed* before being
# accepted, rather than merely located.
PYTHON ?= $(shell for p in python3 python; do \
    "$$p" -c "" >/dev/null 2>&1 && command -v "$$p" && break; done)

# Interpreter *inside* the venv. POSIX puts it in bin/, Windows in Scripts/.
# Absolute, so recipes that cd into a subdirectory still find it.
VENV_PY := $(firstword $(wildcard $(CURDIR)/backend/.venv/bin/python \
                                  $(CURDIR)/backend/.venv/Scripts/python.exe))
PY = $(if $(VENV_PY),$(VENV_PY),$(error No backend venv. Run 'make install' first))

.PHONY: help secrets install test lint fmt migrate revision up down logs ui-dev api-dev doctor

help:
	@echo "No Python needed:"
	@echo "  secrets   Print freshly generated values for .env"
	@echo "  up        Start the full stack"
	@echo "  down      Stop the stack"
	@echo "  logs      Follow api and worker logs"
	@echo "  doctor    Check the tools this Makefile needs"
	@echo ""
	@echo "Local development (needs 'make install' first):"
	@echo "  install   Create the backend venv and install UI deps"
	@echo "  test      Run the backend test suite"
	@echo "  lint      Ruff + TypeScript typecheck"
	@echo "  migrate   Apply migrations to the configured database"
	@echo "  revision  Autogenerate a migration (m=\"message\")"

doctor:
	@echo "python:  $(if $(PYTHON),$(PYTHON),NOT FOUND - apt install python3 python3-venv)"
	@echo "venv:    $(if $(VENV_PY),$(VENV_PY),not created - run 'make install')"
	@if command -v docker >/dev/null 2>&1; then echo "docker:  $$(docker --version)"; \
	 else echo "docker:  NOT FOUND"; fi
	@if docker compose version >/dev/null 2>&1; then echo "compose: $$(docker compose version)"; \
	 else echo "compose: NOT FOUND - needs the docker compose v2 plugin"; fi
	@if command -v node >/dev/null 2>&1; then echo "node:    $$(node --version)"; \
	 else echo "node:    not found (only needed for UI development)"; fi

# Deliberately does not use the venv: generating secrets is the first thing an
# operator does, long before any Python environment exists. Falls back to
# openssl so a bare Docker host with no Python still works.
secrets:
	@if [ -n "$(PYTHON)" ]; then \
		$(PYTHON) -c "import secrets; \
print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24)); \
print('SDWAN_SECRET_KEY=' + secrets.token_urlsafe(48)); \
print('SDWAN_JWT_SECRET=' + secrets.token_urlsafe(48)); \
print('SDWAN_BOOTSTRAP_ADMIN_PASSWORD=' + secrets.token_urlsafe(18))"; \
	elif command -v openssl >/dev/null 2>&1; then \
		echo "POSTGRES_PASSWORD=$$(openssl rand -hex 24)"; \
		echo "SDWAN_SECRET_KEY=$$(openssl rand -hex 48)"; \
		echo "SDWAN_JWT_SECRET=$$(openssl rand -hex 48)"; \
		echo "SDWAN_BOOTSTRAP_ADMIN_PASSWORD=$$(openssl rand -hex 18)"; \
	else \
		echo "Need python3 or openssl to generate secrets." >&2; exit 1; \
	fi

install:
	@test -n "$(PYTHON)" || { \
		echo "python3 not found. On Debian/Ubuntu: apt install python3 python3-venv" >&2; \
		exit 1; }
	$(PYTHON) -m venv backend/.venv
	@# Resolved in the shell, not by make: the venv did not exist at parse time.
	@if [ -x backend/.venv/bin/python ]; then VP=backend/.venv/bin/python; \
	 else VP=backend/.venv/Scripts/python.exe; fi; \
	 "$$VP" -m pip install --upgrade pip && "$$VP" -m pip install -e "backend[dev]"
	@if command -v npm >/dev/null 2>&1; then cd ui && npm install; \
	 else echo "npm not found; skipping UI dependencies"; fi

test:
	cd backend && "$(PY)" -m pytest -q

lint:
	cd backend && "$(PY)" -m ruff check .
	cd ui && npm run lint

fmt:
	cd backend && "$(PY)" -m ruff check --fix .

migrate:
	cd backend && "$(PY)" -m alembic upgrade head

revision:
	cd backend && "$(PY)" -m alembic revision --autogenerate -m "$(m)"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

api-dev:
	cd backend && "$(PY)" -m uvicorn app.main:app --reload --port 8000

ui-dev:
	cd ui && npm run dev
