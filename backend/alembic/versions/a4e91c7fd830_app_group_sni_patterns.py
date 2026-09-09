"""app group sni patterns

M9: TLS SNI matching via RouterOS's tls-host firewall matcher. See
app.render.policy for the two-stage mangle rendering and docs/plan-v2.md M9.

Revision ID: a4e91c7fd830
Revises: f7b3d81a04c2
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a4e91c7fd830'
down_revision: str | None = 'f7b3d81a04c2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('app_groups') as batch:
        batch.add_column(sa.Column('sni_patterns', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('app_groups') as batch:
        batch.drop_column('sni_patterns')
