"""enrollment tokens

M8: one-touch provisioning. An operator mints a token; a field tech pastes
one line into a factory-default router's terminal. See
app.services.enrollment and docs/plan-v2.md M8.

Revision ID: f7b3d81a04c2
Revises: e2c4a9f61b3d
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f7b3d81a04c2'
down_revision: str | None = 'e2c4a9f61b3d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'enrollment_tokens',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('site_name', sa.String(length=128), nullable=False),
        sa.Column('site_role', sa.String(length=16), nullable=False,
                  server_default='spoke'),
        sa.Column('local_prefixes', sa.JSON(), nullable=True),
        sa.Column('fabric_id', sa.String(length=36), nullable=True),
        sa.Column('device_password_enc', sa.Text(), nullable=False),
        sa.Column('source_cidr', sa.String(length=64), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('used_from_ip', sa.String(length=64), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=36), nullable=True),
        sa.Column('enrolled_site_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['fabric_id'], ['fabrics.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['enrolled_site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('prefix', name='uq_enrollment_tokens_prefix'),
    )
    op.create_index(
        op.f('ix_enrollment_tokens_tenant_id'), 'enrollment_tokens', ['tenant_id']
    )
    op.create_index(
        op.f('ix_enrollment_tokens_prefix'), 'enrollment_tokens', ['prefix']
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_enrollment_tokens_prefix'), table_name='enrollment_tokens')
    op.drop_index(op.f('ix_enrollment_tokens_tenant_id'), table_name='enrollment_tokens')
    op.drop_table('enrollment_tokens')
