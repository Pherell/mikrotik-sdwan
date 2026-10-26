"""The apply journal and the scoped pre-apply backups (C3 + C4).

Two records an apply leaves behind besides the Job row:

* **The journal** -- one row per op per apply, plus marker rows for chunk
  commits/aborts and quarantine transitions. The Job row holds the summary
  an operator reads; the journal is the evidence underneath it: which op
  failed, which were skipped because of it, which chunks committed before
  the failure. Rows are flushed as the apply runs (never committed
  mid-apply -- they share the caller's transaction, exactly like the Job
  row itself), so a controller that dies mid-apply takes its uncommitted
  journal with it, but any apply that *finished* has a complete one.

  An op's props are **not** stored. What is stored is the redacted summary
  string built from ``ConfigOp.redacted()`` -- enough to identify the op
  and see its shape, with every secret already masked. A journal that
  kept raw props would be a second copy of every PSK the controller
  manages, in a table nobody thinks to protect.

* **The scoped preimage** -- the controller-owned device rows exactly as
  the plan read them, per apply generation, secrets redacted. This is
  C4's restore point: not the full binary backup (that still happens on
  the device, and is the disaster layer), but the small, diffable record
  of "what our rows looked like before generation N touched them", from
  which an inverse apply can be built. Retention follows the on-device
  backup pattern: the newest two generations per site are kept.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Tenanted, Timestamps, UUIDPk
from app.models.site import JSONCol


class ApplyJournalEntry(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "apply_journal"
    __table_args__ = (
        Index("ix_apply_journal_site_generation", "site_id", "generation"),
    )

    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL, not CASCADE: the journal is evidence about an apply and
    # should outlive the Job row it was written for, exactly as the Job's
    # own site_id does.
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    # The site's apply generation this entry belongs to (see
    # Site.apply_generation). All entries of one push share it.
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # Position of the op within the push, 0-based, matching the order ops
    # were handed to the driver. Null on event rows, which are not ops.
    step: Mapped[int | None] = mapped_column(Integer)
    # 'op' | 'chunk_commit' | 'chunk_abort' | 'quarantine' | 'release'
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    op_kind: Mapped[str | None] = mapped_column(String(16))
    op_path: Mapped[str | None] = mapped_column(String(255))
    # The RouterOS .id the op addressed, when it addressed an existing row
    # (set/remove/move). Adds have none yet.
    op_identity: Mapped[str | None] = mapped_column(String(255))
    op_comment: Mapped[str | None] = mapped_column(String(255))
    # For 'op' rows: 'applied' | 'failed' | 'skipped'. For event rows:
    # 'info'.
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # For 'op' rows: the redacted one-line summary of the op. For event
    # rows: the human-readable detail the caller passed (chunk sizes,
    # quarantine reason, who released what).
    detail: Mapped[str | None] = mapped_column(Text)


class ApplyBackup(Base, UUIDPk, Timestamps, Tenanted):
    __tablename__ = "apply_backups"
    __table_args__ = (
        Index("ix_apply_backups_site_generation", "site_id", "generation"),
    )

    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # Only one kind exists today. The column is here so a future on-device
    # export or full-config snapshot can live in the same table without a
    # second migration, not because two kinds are half-built.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="scoped_preimage")
    # {path: [row, ...]} -- the controller-owned rows the plan read for
    # each menu, with secrets already redacted via redact_props. Rows hold
    # exactly the properties the plan's filtered read requested (identity
    # plus compared props): a restore sketch of our rows, honestly not a
    # full export of the menu.
    payload: Mapped[dict] = mapped_column(JSONCol, nullable=False)
