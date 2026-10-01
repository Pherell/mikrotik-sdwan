"""alerts, alert state, notification channels

Outbound alerting: a log of state transitions (alerts), the last observed
state per watched subject that makes it a log of *transitions* rather than of
every poll (alert_states), and per-tenant webhook / Telegram destinations
with their credentials encrypted at rest (notification_channels). See
app.services.alerts.

Revision ID: c5e2f8a1d903
Revises: c5e2b8d17a93
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c5e2f8a1d903'
down_revision: str | None = 'c5e2b8d17a93'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'alerts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('site_id', sa.String(length=36), nullable=True),
        sa.Column('site_name', sa.String(length=128), nullable=True),
        sa.Column('link_id', sa.String(length=36), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('details', sa.JSON().with_variant(
            postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
        sa.Column('delivery_state', sa.String(length=16), nullable=False,
                  server_default='pending'),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivery_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['link_id'], ['links.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_alerts_tenant_id'), 'alerts', ['tenant_id'])
    op.create_index(op.f('ix_alerts_kind'), 'alerts', ['kind'])
    op.create_index(op.f('ix_alerts_site_id'), 'alerts', ['site_id'])
    op.create_index(op.f('ix_alerts_delivery_state'), 'alerts', ['delivery_state'])
    op.create_index('ix_alerts_tenant_created', 'alerts', ['tenant_id', 'created_at'])

    op.create_table(
        'alert_states',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('subject', sa.String(length=128), nullable=False),
        sa.Column('state', sa.String(length=24), nullable=False),
        sa.Column('site_id', sa.String(length=36), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_alert_states_tenant_id'), 'alert_states', ['tenant_id'])
    op.create_index(op.f('ix_alert_states_site_id'), 'alert_states', ['site_id'])
    op.create_index('ix_alert_states_subject', 'alert_states', ['subject'])

    op.create_table(
        'notification_channels',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('config_enc', sa.Text(), nullable=False),
        sa.Column('target_hint', sa.String(length=255), nullable=False,
                  server_default=''),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('min_severity', sa.String(length=16), nullable=False,
                  server_default='info'),
        sa.Column('last_status', sa.String(length=16), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_channel_tenant_name'),
    )
    op.create_index(
        op.f('ix_notification_channels_tenant_id'), 'notification_channels', ['tenant_id']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_notification_channels_tenant_id'),
                  table_name='notification_channels')
    op.drop_table('notification_channels')
    op.drop_index('ix_alert_states_subject', table_name='alert_states')
    op.drop_index(op.f('ix_alert_states_site_id'), table_name='alert_states')
    op.drop_index(op.f('ix_alert_states_tenant_id'), table_name='alert_states')
    op.drop_table('alert_states')
    op.drop_index('ix_alerts_tenant_created', table_name='alerts')
    op.drop_index(op.f('ix_alerts_delivery_state'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_site_id'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_kind'), table_name='alerts')
    op.drop_index(op.f('ix_alerts_tenant_id'), table_name='alerts')
    op.drop_table('alerts')
