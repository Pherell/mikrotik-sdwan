"""Things that only break inside a container.

`SDWAN_CORS_ORIGINS` broke every deployment while 294 tests passed. The suite
imported the application with an inherited environment and a conftest that
supplied whatever was missing, so it never reproduced the one condition that
matters: a *clean* environment containing exactly what docker-compose.yml sets,
and nothing else.

Every test here runs in a fresh subprocess with a scrubbed environment, which is
what the containers actually get.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
COMPOSE = ROOT / "docker-compose.yml"

# Exactly what docker-compose.yml puts in the api, worker and migrate
# containers. Kept literal rather than parsed so a change to compose that drops
# a variable shows up as a failure here instead of being silently mirrored.
CONTAINER_ENV = {
    "SDWAN_ENV": "prod",
    "SDWAN_DEBUG": "false",
    "SDWAN_DATABASE_URL": "postgresql+asyncpg://sdwan:pw@db:5432/sdwan",
    "SDWAN_REDIS_URL": "redis://redis:6379/0",
    "SDWAN_SECRET_KEY": "a-generated-secret-key-of-adequate-length-here",
    "SDWAN_JWT_SECRET": "a-generated-jwt-secret-of-adequate-length-here",
    "SDWAN_BOOTSTRAP_ADMIN_EMAIL": "admin@local",
    "SDWAN_BOOTSTRAP_ADMIN_PASSWORD": "generated",
    "SDWAN_CORS_ORIGINS": "http://localhost:8080",
}


def run_clean(code: str, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet with only the container's environment.

    PATH and SYSTEMROOT are kept because the interpreter will not start without
    them on Windows; nothing else is inherited, so an accidental dependency on
    the developer's shell fails here.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        # Never let a stray .env in the working tree supply a value the
        # container would not have.
        "SDWAN_ENV_FILE_DISABLED": "1",
        **CONTAINER_ENV,
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# -- the entrypoints each container runs ------------------------------------


def test_the_api_entrypoint_imports() -> None:
    """`uvicorn app.main:app` -- if this raises, the api container never starts."""
    r = run_clean("import app.main; assert app.main.app")
    assert r.returncode == 0, r.stderr[-2000:]


def test_the_worker_entrypoint_imports() -> None:
    """`arq app.tasks.worker.WorkerSettings`. Its redis_settings is evaluated at
    class-definition time, so a bad SDWAN_REDIS_URL fails at import."""
    r = run_clean(
        "from app.tasks.worker import WorkerSettings;"
        " assert WorkerSettings.redis_settings.host == 'redis'"
    )
    assert r.returncode == 0, r.stderr[-2000:]


def test_alembics_env_py_loads() -> None:
    """The exact failure that broke the deployment: alembic/env.py calls
    get_settings() at module level, so a settings error kills `migrate`."""
    r = run_clean(
        "from alembic.config import Config;"
        " from alembic.script import ScriptDirectory;"
        " c = Config('alembic.ini');"
        " ScriptDirectory.from_config(c).get_current_head()"
    )
    assert r.returncode == 0, r.stderr[-2000:]


def test_migrate_runs_end_to_end(tmp_path: Path) -> None:
    """`alembic upgrade head`, the migrate container's whole job."""
    db = tmp_path / "parity.db"
    r = run_clean(
        "from alembic import command;"
        " from alembic.config import Config;"
        " command.upgrade(Config('alembic.ini'), 'head')",
        SDWAN_DATABASE_URL=f"sqlite+aiosqlite:///{db.as_posix()}",
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert db.exists()


# -- generalised guards against the same class of bug -----------------------


def test_no_complex_setting_decodes_from_the_environment_unguarded() -> None:
    """The actual root cause, generalised.

    pydantic-settings JSON-decodes list/dict/set-typed environment variables
    inside the settings source, before field validators run. Any such field
    without NoDecode will raise on a plain string -- which is the only form an
    env var realistically carries.
    """
    r = run_clean(
        "import typing;"
        " from pydantic_settings import NoDecode;"
        " from app.config import Settings;"
        " bad = [n for n, f in Settings.model_fields.items()"
        "        if typing.get_origin(f.annotation) in (list, dict, set, tuple)"
        "        and NoDecode not in f.metadata];"
        " print(bad);"
        " assert not bad, bad"
    )
    assert r.returncode == 0, (
        f"complex settings fields missing NoDecode: {r.stdout.strip()}\n{r.stderr[-800:]}"
    )


def test_every_required_setting_is_supplied_by_compose() -> None:
    """A field with no default that compose does not set means the container
    cannot start, no matter how green the suite is."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    supplied = set()
    for block in compose.get("x-backend-env", {}):
        supplied.add(block)
    for service in compose.get("services", {}).values():
        env = service.get("environment")
        if isinstance(env, dict):
            supplied.update(env)

    r = run_clean(
        "from app.config import Settings;"
        " print(','.join('SDWAN_' + n.upper()"
        "   for n, f in Settings.model_fields.items() if f.is_required()))"
    )
    assert r.returncode == 0, r.stderr[-800:]
    required = {v for v in r.stdout.strip().split(",") if v}

    assert required <= supplied, f"compose does not set: {sorted(required - supplied)}"


def test_exactly_one_migration_head() -> None:
    """Two heads make `alembic upgrade head` ambiguous and the container exits 1."""
    r = run_clean(
        "from alembic.config import Config;"
        " from alembic.script import ScriptDirectory;"
        " print(len(ScriptDirectory.from_config(Config('alembic.ini')).get_heads()))"
    )
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip() == "1", f"expected one head, found {r.stdout.strip()}"


# -- the image would contain what runtime needs -----------------------------


@pytest.mark.parametrize("required", ["pyproject.toml", "alembic.ini", "alembic", "app"])
def test_the_dockerfile_copies_what_runtime_needs(required: str) -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    copied = re.findall(r"^COPY\s+(\S+)", dockerfile, re.M)
    assert required in copied, f"Dockerfile never copies {required}"


def test_dockerignore_excludes_nothing_runtime_needs() -> None:
    ignored = {
        line.strip()
        for line in (BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for needed in ("app", "alembic", "alembic.ini", "pyproject.toml"):
        assert needed not in ignored, f".dockerignore excludes {needed}"
