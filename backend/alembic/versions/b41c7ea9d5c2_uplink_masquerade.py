"""uplink masquerade

Steering sends traffic out an uplink the site's own NAT rules were never
written for, so it leaves with a private source address and dies upstream.
The controller can emit the masquerade rule -- but not unconditionally: an
uplink onto private transit (MPLS, a partner link) must not be NAT'd, and
guessing wrong there is worse than not guessing.

Defaults to true because a Wan is an internet uplink in almost every case.

Revision ID: b41c7ea9d5c2
Revises: a63884a33993
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b41c7ea9d5c2'
down_revision: str | None = 'a63884a33993'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'wans',
        sa.Column(
            'masquerade',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column('wans', 'masquerade')
