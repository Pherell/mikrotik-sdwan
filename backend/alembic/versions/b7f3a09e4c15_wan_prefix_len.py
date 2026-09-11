"""uplink mask length

The controller stored an uplink's address with no mask, which is lossy in a
way that matters: it is the mask that says whether a tunnel's far endpoint
sits on this segment or is reached through the gateway, and those need
different routes. See app.render.fabric._underlay_routes.

Existing rows stay null and are filled by the next probe. A null mask is not
fatal -- the route falls back to the gateway form, which is correct off-link
and merely hairpins on-link.

Revision ID: b7f3a09e4c15
Revises: a4e91c2d7b60
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7f3a09e4c15'
down_revision: str | None = 'a4e91c2d7b60'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('wans') as batch:
        batch.add_column(sa.Column('prefix_len', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('wans') as batch:
        batch.drop_column('prefix_len')
