"""SLA anti-flap counters, failback margins, probe shape (A3+A5)

SlaProfile gains the plan A3 knobs (fail/restore consecutive-test counts
and failback hysteresis margins) and the plan A5 probe shape (probe type,
service port, extra agreement targets). Existing rows take the defaults,
which is the A3/A5 behaviour: fail x5 / restore x5, 10 ms / 5 ms margins,
icmp probes of the hop alone. See app.models.policy.

Revision ID: 28804be7e407
Revises: d04f97a78d74
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '28804be7e407'
down_revision: str | None = 'd04f97a78d74'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('sla_profiles') as batch:
        batch.add_column(
            sa.Column(
                'fail_count', sa.Integer(), nullable=False, server_default='5'
            )
        )
        batch.add_column(
            sa.Column(
                'restore_count', sa.Integer(), nullable=False, server_default='5'
            )
        )
        batch.add_column(
            sa.Column(
                'failback_latency_margin_ms',
                sa.Integer(),
                nullable=False,
                server_default='10',
            )
        )
        batch.add_column(
            sa.Column(
                'failback_jitter_margin_ms',
                sa.Integer(),
                nullable=False,
                server_default='5',
            )
        )
        batch.add_column(
            sa.Column(
                'probe_type',
                sa.String(length=16),
                nullable=False,
                server_default='icmp',
            )
        )
        batch.add_column(sa.Column('probe_port', sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column('probe_dns_name', sa.String(length=255), nullable=True)
        )
        batch.add_column(sa.Column('probe_targets', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sla_profiles') as batch:
        batch.drop_column('probe_targets')
        batch.drop_column('probe_dns_name')
        batch.drop_column('probe_port')
        batch.drop_column('probe_type')
        batch.drop_column('failback_jitter_margin_ms')
        batch.drop_column('failback_latency_margin_ms')
        batch.drop_column('restore_count')
        batch.drop_column('fail_count')
