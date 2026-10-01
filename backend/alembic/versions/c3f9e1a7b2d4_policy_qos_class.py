"""policy QoS class

A policy may now name a QoS class (realtime / interactive / default / bulk)
that render.qos turns into a packet mark and a per-uplink queue-tree leaf.
Nullable, and null means "no QoS": existing policies keep rendering exactly
what they did before, and no queue tree appears on any device until an
operator sets both a class and an uplink bandwidth.

Revision ID: c3f9e1a7b2d4
Revises: c5e2f8a1d903
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f9e1a7b2d4'
down_revision: str | None = 'c5e2f8a1d903'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('policies') as batch:
        batch.add_column(sa.Column('qos_class', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('policies') as batch:
        batch.drop_column('qos_class')
