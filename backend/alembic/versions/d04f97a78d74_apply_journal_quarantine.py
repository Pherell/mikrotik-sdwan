"""apply journal, quarantine, scoped preimage backups (B1+C3+C4)

Sites gain the C3 quarantine state and the B1 Safe Mode SSH port; two new
tables hold the per-apply journal and the scoped preimage backups. See
app.models.apply_journal and app.models.site.

Revision ID: d04f97a78d74
Revises: 04ce3bccc512
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd04f97a78d74'
down_revision: str | None = '04ce3bccc512'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('sites') as batch:
        batch.add_column(sa.Column('ssh_port', sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                'quarantined', sa.Boolean(),
                nullable=False, server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column('quarantine_reason', sa.Text(), nullable=True))
        batch.add_column(
            sa.Column('quarantined_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                'apply_generation', sa.Integer(),
                nullable=False, server_default='0',
            )
        )

    op.create_table(
        'apply_journal',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('site_id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('step', sa.Integer(), nullable=True),
        sa.Column('event', sa.String(length=32), nullable=False),
        sa.Column('op_kind', sa.String(length=16), nullable=True),
        sa.Column('op_path', sa.String(length=255), nullable=True),
        sa.Column('op_identity', sa.String(length=255), nullable=True),
        sa.Column('op_comment', sa.String(length=255), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_apply_journal_tenant_id'), 'apply_journal', ['tenant_id'])
    op.create_index(op.f('ix_apply_journal_site_id'), 'apply_journal', ['site_id'])
    op.create_index(op.f('ix_apply_journal_job_id'), 'apply_journal', ['job_id'])
    op.create_index(
        'ix_apply_journal_site_generation', 'apply_journal', ['site_id', 'generation']
    )

    op.create_table(
        'apply_backups',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False,
                  server_default='default'),
        sa.Column('site_id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=True),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_apply_backups_tenant_id'), 'apply_backups', ['tenant_id'])
    op.create_index(op.f('ix_apply_backups_site_id'), 'apply_backups', ['site_id'])
    op.create_index(op.f('ix_apply_backups_job_id'), 'apply_backups', ['job_id'])
    op.create_index(
        'ix_apply_backups_site_generation', 'apply_backups', ['site_id', 'generation']
    )


def downgrade() -> None:
    op.drop_index('ix_apply_backups_site_generation', table_name='apply_backups')
    op.drop_index(op.f('ix_apply_backups_job_id'), table_name='apply_backups')
    op.drop_index(op.f('ix_apply_backups_site_id'), table_name='apply_backups')
    op.drop_index(op.f('ix_apply_backups_tenant_id'), table_name='apply_backups')
    op.drop_table('apply_backups')

    op.drop_index('ix_apply_journal_site_generation', table_name='apply_journal')
    op.drop_index(op.f('ix_apply_journal_job_id'), table_name='apply_journal')
    op.drop_index(op.f('ix_apply_journal_site_id'), table_name='apply_journal')
    op.drop_index(op.f('ix_apply_journal_tenant_id'), table_name='apply_journal')
    op.drop_table('apply_journal')

    with op.batch_alter_table('sites') as batch:
        batch.drop_column('apply_generation')
        batch.drop_column('quarantined_at')
        batch.drop_column('quarantine_reason')
        batch.drop_column('quarantined')
        batch.drop_column('ssh_port')
