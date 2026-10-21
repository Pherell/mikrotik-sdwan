"""central SLA evaluations

A1: the poller already reads netwatch's counters back into samples; this is
the persisted verdict the evaluator (app.telemetry.evaluator) derives from
them -- one row per path per bucket, which doubles as the Fase 2b compliance
record. SlaProfile also gains the window the evaluator should judge it over.
See app.models.telemetry.SlaEvaluation.

Revision ID: 04ce3bccc512
Revises: b7f3a09e4c15
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '04ce3bccc512'
down_revision: str | None = 'b7f3a09e4c15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'sla_evaluations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('site_id', sa.String(length=36), nullable=False),
        sa.Column('link_id', sa.String(length=36), nullable=False),
        sa.Column('sdwan_group_id', sa.String(length=36), nullable=True),
        sa.Column('sla_profile_id', sa.String(length=36), nullable=True),
        sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_minutes', sa.Integer(), nullable=False),
        sa.Column('loss_percent', sa.Float(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('jitter_ms', sa.Float(), nullable=True),
        sa.Column('classification', sa.String(length=16), nullable=False),
        sa.Column('previous_class', sa.String(length=16), nullable=True),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['link_id'], ['links.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['sdwan_group_id'], ['sdwan_groups.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['sla_profile_id'], ['sla_profiles.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'site_id', 'link_id', 'sdwan_group_id', 'bucket_start',
            name='uq_sla_eval_path_bucket',
        ),
    )
    op.create_index(op.f('ix_sla_evaluations_tenant_id'), 'sla_evaluations', ['tenant_id'])
    op.create_index(op.f('ix_sla_evaluations_site_id'), 'sla_evaluations', ['site_id'])
    op.create_index(op.f('ix_sla_evaluations_link_id'), 'sla_evaluations', ['link_id'])
    op.create_index(
        'ix_sla_evaluations_link_bucket', 'sla_evaluations', ['link_id', 'bucket_start']
    )
    op.create_index(
        'ix_sla_evaluations_site_bucket', 'sla_evaluations', ['site_id', 'bucket_start']
    )

    with op.batch_alter_table('sla_profiles') as batch:
        batch.add_column(
            sa.Column(
                'eval_window_minutes', sa.Integer(),
                nullable=False, server_default='30',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('sla_profiles') as batch:
        batch.drop_column('eval_window_minutes')

    op.drop_index('ix_sla_evaluations_site_bucket', table_name='sla_evaluations')
    op.drop_index('ix_sla_evaluations_link_bucket', table_name='sla_evaluations')
    op.drop_index(op.f('ix_sla_evaluations_link_id'), table_name='sla_evaluations')
    op.drop_index(op.f('ix_sla_evaluations_site_id'), table_name='sla_evaluations')
    op.drop_index(op.f('ix_sla_evaluations_tenant_id'), table_name='sla_evaluations')
    op.drop_table('sla_evaluations')
