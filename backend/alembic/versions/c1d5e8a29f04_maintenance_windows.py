"""maintenance windows

M10: queue an apply for an approved window instead of pushing it now. See
app.models.job.Job.scheduled_for/window_closes_at and
app.tasks.worker.run_scheduled_applies.

Revision ID: c1d5e8a29f04
Revises: b8f204e6c917
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d5e8a29f04'
down_revision: str | None = 'b8f204e6c917'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('window_closes_at', sa.DateTime(timezone=True), nullable=True))
        batch.create_index(op.f('ix_jobs_scheduled_for'), ['scheduled_for'])


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_index(op.f('ix_jobs_scheduled_for'))
        batch.drop_column('window_closes_at')
        batch.drop_column('scheduled_for')
