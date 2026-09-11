"""per-link UDP listen port

WireGuard binds one UDP listener per interface and the renderer emits one
interface per link, so a multi-homed site asked for two listeners on the one
fabric-wide port. RouterOS accepts the second row and leaves it running=false
without complaining, so the tunnel simply never came up. The port moves onto
the link. See app.fabric.allocate.allocate_listen_port.

Existing rows are left null and backfilled by the next expansion, which is the
only place that knows which ports a site's other fabrics already hold.

Revision ID: a4e91c2d7b60
Revises: c1d5e8a29f04
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a4e91c2d7b60'
down_revision: str | None = 'c1d5e8a29f04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('links') as batch:
        batch.add_column(sa.Column('listen_port', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('links') as batch:
        batch.drop_column('listen_port')
