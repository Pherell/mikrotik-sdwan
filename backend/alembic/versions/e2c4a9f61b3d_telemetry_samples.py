"""telemetry samples

M7: read netwatch and system/resource back into a table instead of throwing
them away. See app.telemetry.poller and docs/plan-v2.md M7.

Revision ID: e2c4a9f61b3d
Revises: d3a81f5c6e40
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e2c4a9f61b3d'
down_revision: str | None = 'd3a81f5c6e40'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'samples',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('site_id', sa.String(length=36), nullable=False),
        sa.Column('link_id', sa.String(length=36), nullable=True),
        sa.Column('metric', sa.String(length=32), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['link_id'], ['links.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_samples_tenant_id'), 'samples', ['tenant_id'])
    op.create_index(op.f('ix_samples_site_id'), 'samples', ['site_id'])
    op.create_index(op.f('ix_samples_link_id'), 'samples', ['link_id'])
    op.create_index(
        'ix_samples_link_metric_time', 'samples', ['link_id', 'metric', 'collected_at']
    )
    op.create_index(
        'ix_samples_site_metric_time', 'samples', ['site_id', 'metric', 'collected_at']
    )


def downgrade() -> None:
    op.drop_index('ix_samples_site_metric_time', table_name='samples')
    op.drop_index('ix_samples_link_metric_time', table_name='samples')
    op.drop_index(op.f('ix_samples_link_id'), table_name='samples')
    op.drop_index(op.f('ix_samples_site_id'), table_name='samples')
    op.drop_index(op.f('ix_samples_tenant_id'), table_name='samples')
    op.drop_table('samples')
