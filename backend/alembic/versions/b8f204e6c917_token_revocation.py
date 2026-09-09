"""token revocation

M10: users.tokens_valid_after. A JWT carries no server-side row to revoke;
what can be recorded is "nothing issued before this instant is valid any
more". See app.models.user and app.deps.current_user.

Revision ID: b8f204e6c917
Revises: a4e91c7fd830
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b8f204e6c917'
down_revision: str | None = 'a4e91c7fd830'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('tokens_valid_after', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('tokens_valid_after')
