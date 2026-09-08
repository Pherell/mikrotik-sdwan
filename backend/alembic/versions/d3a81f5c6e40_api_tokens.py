"""api tokens

A credential that is not a person. Until now the only one was a user's login
JWT, so every script ran as somebody with that somebody's full rights, expired
when their session did, and could not be revoked without disabling the human.

The permission is a role rather than a separate scope vocabulary: the product
already has three roles that mean something, and a second orthogonal system
next to them is two models that have to agree and eventually do not.

Revision ID: d3a81f5c6e40
Revises: c92f4d1ab730
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3a81f5c6e40"
down_revision: str | None = "c92f4d1ab730"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        # The public half: unique and indexed, so verification is one indexed
        # read rather than a scan that hashes every stored token.
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        # Revocation is a timestamp, not a delete: a token that authorised
        # something last week must still be nameable in the audit trail.
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_api_tokens_prefix", "api_tokens", ["prefix"], unique=True)
    op.create_index("ix_api_tokens_owner_id", "api_tokens", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_owner_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_prefix", table_name="api_tokens")
    op.drop_table("api_tokens")
