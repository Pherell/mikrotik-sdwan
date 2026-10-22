"""one live apply per site

The apply endpoint's "is an apply already queued or running?" check is
check-then-act inside a transaction that only commits at the end of the
request, so two concurrent applies to the same site both pass it and push
to the same device: two dead-man rollback schedulers get armed, and their
pre-apply backups can delete each other. The scheduled-apply sweep and
drift auto-remediation never checked at all.

This partial unique index makes the database the arbiter: at most one
apply job in queued or running state may exist per site. The application
code (api/v1/jobs.py, tasks/worker.py, services/drift.py) commits its claim
before pushing so the common case fails fast with a 409; the index is the
backstop for the exact race where two claims slip through simultaneously.

Deploy note: if the index creation fails, the database already holds two
live applies for one site (e.g. a job stuck in `running` after a crash).
Resolve or delete the stale row first, then re-run the migration.

Revision ID: d24d4fff23a5
Revises: b7f3a09e4c15
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd24d4fff23a5'
down_revision: str | None = 'b7f3a09e4c15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Partial index: supported by both SQLite and PostgreSQL. IF NOT EXISTS /
    # IF EXISTS keep the migration re-runnable on either.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_one_active_apply_per_site "
            "ON jobs (site_id) "
            "WHERE kind = 'apply' AND state IN ('queued', 'running')"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_jobs_one_active_apply_per_site"))
