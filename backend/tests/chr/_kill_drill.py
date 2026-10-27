"""Subprocess driven by the kill-mid-apply chaos drill (plan C1 3a).

Run as ``python -m tests.chr._kill_drill <site-name>`` from backend/.
It mirrors the production worker's shape: the Job row is created and
committed first (so it is visible to other sessions), then
``apply_site`` runs in a second transaction that only commits when the
apply finishes. The parent test SIGKILLs this process mid-push and
inspects what that leaves behind -- on the device and in the DB.

Environment: CHR_PASSWORD / CHR_DB_URL as in lab_topology, plus
SDWAN_SAFE_MODE_CHUNK_SIZE (the parent sets it small so the push has
many chunk boundaries to be killed between). The secret-key defaults
below match tests/conftest.py so the subprocess decrypts site
credentials exactly like the pytest process that seeded them.
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("SDWAN_SECRET_KEY", "test-secret-key-at-least-32-chars-long!!")
os.environ.setdefault("SDWAN_JWT_SECRET", "test-jwt-secret-at-least-32-chars-long!!")

from sqlalchemy import select  # noqa: E402

from app.models.enums import JobKind  # noqa: E402
from app.models.site import Site  # noqa: E402
from app.services.reconcile import apply_site, new_job  # noqa: E402
from tests.chr.lab_topology import sessionmaker_for  # noqa: E402


async def main(site_name: str) -> None:
    sm = sessionmaker_for()
    async with sm() as s:
        site = await s.scalar(select(Site).where(Site.name == site_name))
        assert site is not None, f"no site {site_name}"
        job = new_job(site, JobKind.apply, None)
        s.add(job)
        await s.commit()
        print(f"KILLDRILL job={job.id} site={site_name}", flush=True)
        job = await apply_site(s, site, job)
        await s.commit()
        print(f"KILLDRILL done state={job.state}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
