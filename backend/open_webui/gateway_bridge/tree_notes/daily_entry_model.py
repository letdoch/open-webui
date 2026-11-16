"""SQLAlchemy 2.0 ORM model for daily calendar root note subtype entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.internal.db import Base

if TYPE_CHECKING:
    from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode


class TreeNoteDailyEntry(Base):
    """Binds a daily_note root node to at most one calendar YYYY-MM-DD per user."""

    __tablename__ = 'tree_note_daily_entry'
    __table_args__ = (
        ForeignKeyConstraint(
            ['node_id', 'owner_user_id', 'node_kind'],
            [
                TreeNoteDigest.col_ref('tree_note_node.id'),
                TreeNoteDigest.col_ref('tree_note_node.owner_user_id'),
                TreeNoteDigest.col_ref('tree_note_node.node_kind'),
            ],
            ondelete='CASCADE',
            name='fk_tree_note_daily_node_owner_kind',
        ),
        UniqueConstraint('owner_user_id', 'note_date', name='uq_tree_note_daily_owner_date'),
        CheckConstraint("node_kind = 'daily_note'", name='ck_tree_note_daily_kind'),
        CheckConstraint(
            "length(note_date) = 10 AND substr(note_date, 5, 1) = '-' AND substr(note_date, 8, 1) = '-' "
            "AND substr(note_date, 1, 1) BETWEEN '0' AND '9' AND substr(note_date, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(note_date, 3, 1) BETWEEN '0' AND '9' AND substr(note_date, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(note_date, 6, 1) BETWEEN '0' AND '1' AND substr(note_date, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(note_date, 9, 1) BETWEEN '0' AND '3' AND substr(note_date, 10, 1) BETWEEN '0' AND '9' "
            "AND substr(note_date, 6, 2) BETWEEN '01' AND '12' AND substr(note_date, 9, 2) BETWEEN '01' AND '31'",
            name='ck_tree_note_daily_date_shape',
        ),
    )

    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    node_kind: Mapped[str] = mapped_column(String(16), nullable=False, default='daily_note')
    note_date: Mapped[str] = mapped_column(String(10), nullable=False)

    node: Mapped[TreeNoteNode] = relationship(
        'TreeNoteNode',
        back_populates='daily_entry',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteDailyEntry.node_id)',
        passive_deletes=True,
    )
