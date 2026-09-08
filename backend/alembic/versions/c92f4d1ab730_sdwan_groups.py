"""sdwan groups

Splits the half of a steering rule worth naming -- which uplinks, in what
order, and how healthy they must be -- out of the rule and into a reusable
group, the way Sophos separates an SD-WAN profile from an SD-WAN route.

Every existing rule is moved onto a group built from its own prefer_tags and
SLA, so nothing changes behaviourally on upgrade. Rules that already agree on
both share one group rather than each getting a near-duplicate.

prefer_tags and sla_profile_id are deliberately left on policies: dropping them
would make this migration lossy in one direction, and they cost nothing.

Revision ID: c92f4d1ab730
Revises: b41c7ea9d5c2
Create Date: 2026-09-08
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c92f4d1ab730'
down_revision: str | None = 'b41c7ea9d5c2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'sdwan_groups',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('members', sa.JSON(), nullable=True),
        sa.Column('strategy', sa.String(length=16), nullable=False,
                  server_default='failover'),
        sa.Column('sla_profile_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['sla_profile_id'], ['sla_profiles.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_sdwan_group_tenant_name'),
    )
    op.create_index(op.f('ix_sdwan_groups_name'), 'sdwan_groups', ['name'])
    op.create_index(op.f('ix_sdwan_groups_tenant_id'), 'sdwan_groups', ['tenant_id'])

    with op.batch_alter_table('policies') as batch:
        batch.add_column(sa.Column('sdwan_group_id', sa.String(length=36), nullable=True))
        batch.create_index(op.f('ix_policies_sdwan_group_id'), ['sdwan_group_id'])
        batch.create_foreign_key(
            'fk_policies_sdwan_group', 'sdwan_groups', ['sdwan_group_id'], ['id'],
            ondelete='RESTRICT',
        )

    _move_rules_onto_groups()


def _move_rules_onto_groups() -> None:
    """One group per distinct (uplinks, SLA) pair, shared by the rules using it."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, name, prefer_tags, sla_profile_id FROM policies"
        )
    ).fetchall()

    seen: dict[tuple, str] = {}
    used_names: set[tuple[str, str]] = set()

    for policy_id, tenant_id, policy_name, prefer_tags, sla_profile_id in rows:
        tags = prefer_tags
        if isinstance(tags, str):
            tags = json.loads(tags or "[]")
        tags = list(tags or [])
        if not tags:
            # Nothing to build a path from; the rule was already inert.
            continue

        key = (tenant_id, tuple(tags), sla_profile_id)
        group_id = seen.get(key)
        if group_id is None:
            base = "-".join(str(t) for t in tags)[:100] or policy_name
            name = base
            suffix = 2
            while (tenant_id, name) in used_names:
                name = f"{base}-{suffix}"
                suffix += 1
            used_names.add((tenant_id, name))

            group_id = str(uuid.uuid4())
            bind.execute(
                sa.text(
                    "INSERT INTO sdwan_groups"
                    " (id, tenant_id, name, description, members, strategy,"
                    "  sla_profile_id, created_at, updated_at)"
                    " VALUES (:id, :tenant, :name, :description, :members,"
                    " 'failover', :sla, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": group_id,
                    "tenant": tenant_id,
                    "name": name,
                    "description": "Created automatically from an existing rule.",
                    "members": json.dumps(
                        [{"uplink": str(t), "weight": 1} for t in tags]
                    ),
                    "sla": sla_profile_id,
                },
            )
            seen[key] = group_id

        bind.execute(
            sa.text("UPDATE policies SET sdwan_group_id = :g WHERE id = :p"),
            {"g": group_id, "p": policy_id},
        )


def downgrade() -> None:
    with op.batch_alter_table('policies') as batch:
        batch.drop_constraint('fk_policies_sdwan_group', type_='foreignkey')
        batch.drop_index(op.f('ix_policies_sdwan_group_id'))
        batch.drop_column('sdwan_group_id')
    op.drop_index(op.f('ix_sdwan_groups_tenant_id'), table_name='sdwan_groups')
    op.drop_index(op.f('ix_sdwan_groups_name'), table_name='sdwan_groups')
    op.drop_table('sdwan_groups')
