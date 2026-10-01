"""site uplink_sync policy

Uplink detection used to run once, at probe or enrollment. A periodic
re-detection (app.services.uplinks) now compares what the device shows with
the stored uplinks; this column says what it may do per site: 'off',
'report' (the default -- differences are reported, nothing written) or
'auto' (dynamic uplinks' facts are kept current, new uplinks are added
disabled for review; nothing is ever pushed).

Existing sites get 'report' via the server default, so upgrading changes
nothing on any device and writes nothing to any uplink.

Revision ID: c5e2b8d17a93
Revises: b7f3a09e4c15
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c5e2b8d17a93'
down_revision: str | None = 'b7f3a09e4c15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('sites') as batch:
        batch.add_column(
            sa.Column(
                'uplink_sync', sa.String(length=16), nullable=False, server_default='report'
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('sites') as batch:
        batch.drop_column('uplink_sync')
