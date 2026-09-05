FROM python:3.14-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps for asyncpg/cryptography wheels that lack a manylinux build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, with the source deliberately absent, so this layer stays
# cached across code changes. setuptools finds no packages here and builds an
# empty wheel -- that is fine, the point of this step is the dependency install.
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel && pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Now register the package itself. Without this the app is importable only
# because the working directory happens to be /app, which breaks the moment
# anything runs from elsewhere.
RUN pip install --no-deps -e .

# Run as a non-root user: this container holds decryptable device credentials.
RUN useradd --create-home --uid 10001 sdwan && chown -R sdwan:sdwan /app
USER sdwan

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
