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


# -- the edge: Caddy and compose have to agree -------------------------------
#
# `SDWAN_DOMAIN` reached the Caddy container only because someone remembered to
# list it in compose. Nothing checked, so the next variable added to the
# Caddyfile would have silently taken its default and the stack would have gone
# on answering to the wrong name.

CADDYFILE = ROOT / "Caddyfile"
ENV_EXAMPLE = ROOT / ".env.example"

# "${HTTP_PORT:-80}:${HTTP_PORT:-80}" -- both halves captured so they can be
# compared. A port mapping is not a plain "a:b" split; the ":-" inside the
# default makes that ambiguous.
_PORT_MAPPING = re.compile(r"^\$\{(\w+):-(\d+)\}:\$\{(\w+):-(\d+)\}$")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_every_caddyfile_variable_is_supplied_by_compose() -> None:
    """A {$VAR} the compose file forgets does not fail -- it quietly falls back
    to its default, which is how the whole stack ended up reachable only as
    'localhost' regardless of what the operator configured."""
    used = set(re.findall(r"\{\$([A-Za-z_][A-Za-z0-9_]*)", CADDYFILE.read_text(encoding="utf-8")))
    supplied = set(_compose()["services"]["caddy"].get("environment", {}))
    missing = sorted(used - supplied)
    assert not missing, f"Caddyfile reads variables compose never sets: {missing}"


def test_the_published_port_equals_the_port_caddy_binds() -> None:
    """Caddy builds its redirects and its links from the port it is listening
    on, with no idea a mapping happened. Publish 8443->443 and it sends browsers
    to https://host:443, where nothing is listening."""
    for mapping in _compose()["services"]["caddy"]["ports"]:
        m = _PORT_MAPPING.match(mapping)
        assert m, f"port mapping {mapping!r} is not ${{VAR:-default}} on both sides"
        outside_var, outside_default, inside_var, inside_default = m.groups()
        assert outside_var == inside_var, (
            f"{mapping}: published and bound ports use different variables"
        )
        assert outside_default == inside_default, f"{mapping}: published and bound defaults differ"


def test_the_redirect_carries_the_configured_https_port() -> None:
    """Caddy's own HTTP->HTTPS redirect drops any non-standard port, so the
    Caddyfile turns it off and writes its own. Removing either half without the
    other silently breaks plain-http arrivals."""
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    assert "auto_https disable_redirects" in caddyfile
    assert re.search(r"redir\s+https://\{host\}:\{\$SDWAN_HTTPS_PORT", caddyfile), (
        "the manual redirect must carry the configured HTTPS port"
    )


def test_compose_port_defaults_match_the_documented_ones() -> None:
    """Someone reading .env.example and someone reading docker-compose.yml must
    not come away with different answers."""
    documented = dict(
        re.findall(r"^(HTTP_PORT|HTTPS_PORT)=(\d+)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    )
    defaults = {
        m.group(1): m.group(2)
        for mapping in _compose()["services"]["caddy"]["ports"]
        if (m := _PORT_MAPPING.match(mapping))
    }
    assert documented == defaults, f".env.example says {documented}, compose defaults to {defaults}"


def test_a_service_that_serves_no_http_does_not_inherit_the_api_healthcheck() -> None:
    """One image, three containers. The Dockerfile's HEALTHCHECK curls
    /healthz, which only the api serves -- so the worker reported unhealthy
    forever and `docker compose up --wait` failed on a stack that was fine."""
    for name, service in _compose()["services"].items():
        if service.get("build") != "./backend":
            continue
        command = service.get("command") or []
        if command and command[0] == "uvicorn":
            continue  # this one really does serve HTTP
        if not command:
            continue  # no override: runs the image's uvicorn CMD
        healthcheck = service.get("healthcheck")
        assert healthcheck, (
            f"{name} runs {command[0]}, not uvicorn, but inherits the HTTP healthcheck"
        )
        if healthcheck.get("disable"):
            continue
        assert "healthz" not in " ".join(healthcheck["test"]), (
            f"{name} runs {command[0]} but its healthcheck still polls the API's /healthz"
        )


# -- the suite has to behave the same wherever it is invoked from -----------


def test_pytest_can_import_the_tests_package_without_python_dash_m() -> None:
    """`python -m pytest` puts the rootdir on sys.path; the bare `pytest`
    console script does not. conftest.py imports tests.fakeros, so the Makefile
    (which used the first form) passed while CI (which used the second) failed
    to collect anything at all, on every push."""
    pyproject = (BACKEND / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^pythonpath = \[\".\"\]$", pyproject, re.M), (
        "pytest needs pythonpath = [\".\"] or the console script cannot import tests.fakeros"
    )
