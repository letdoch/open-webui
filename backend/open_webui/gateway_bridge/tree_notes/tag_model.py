"""SQLAlchemy 2.0 ORM model for normalized tree note tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.internal.db import Base

if TYPE_CHECKING:
    from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode


class TreeNoteTag(Base):
    """Normalized many-to-many lowercase tag slug attached to a tree_note_node."""

    __tablename__ = 'tree_note_tag'
    __table_args__ = (
        ForeignKeyConstraint(
            ['node_id'],
            [TreeNoteDigest.col_ref('tree_note_node.id')],
            ondelete='CASCADE',
            name='fk_tree_note_tag_node',
        ),
        CheckConstraint(
            'length(tag_slug) BETWEEN 1 AND 64 '
            'AND tag_slug = lower(tag_slug) '
            'AND tag_slug = trim(tag_slug) '
            "AND NOT (tag_slug LIKE '% %')",
            name='ck_tree_note_tag_slug',
        ),
        Index('ix_tree_note_tag_slug', 'tag_slug', 'node_id'),
    )

    node_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tag_slug: Mapped[str] = mapped_column(String(64), primary_key=True)

    node: Mapped[TreeNoteNode] = relationship(
        'TreeNoteNode',
        back_populates='tags',
        foreign_keys=[node_id],
        passive_deletes=True,
    )
