"""Fixtures shared by the C1 live modules (topology + chaos drills)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from app.models.fabric import Link
from tests.chr.lab_topology import seed_topology, sessionmaker_for


def pytest_collection_modifyitems(items) -> None:
    # Whole-directory gate: every test here needs the live lab, even
    # the ones that never touch the topology fixture (the Safe Mode
    # drills drive a CHR directly). Scoped to this directory -- the
    # hook receives every item in the session.
    if os.environ.get("CHR_LIVE") != "1":
        here = os.path.dirname(os.path.abspath(__file__))
        skip = pytest.mark.skip(reason="CHR_LIVE not set (live CHR lab)")
        for item in items:
            if os.path.dirname(os.path.abspath(str(item.fspath))) == here:
                item.add_marker(skip)


def skip_reason() -> str | None:
    if os.environ.get("CHR_LIVE") != "1":
        return "live CHR tests are opt-in: set CHR_LIVE=1 (see tests/chr/README.md)"
    if not os.environ.get("CHR_PASSWORD"):
        return "CHR_PASSWORD is not set"
    return None


@pytest.fixture
def sm():
    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)
    return sessionmaker_for()


@pytest.fixture
async def topology(sm):
    """Seed (or reuse) the hub+2-spoke lab topology in the controller DB."""
    sites = await seed_topology(sm)
    async with sm() as s:
        links = (await s.scalars(select(Link))).all()
    return {"sites": sites, "links": links}
