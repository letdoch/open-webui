"""SQLAlchemy 2.0 ORM model for addressable Markdown block parts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.internal.db import Base

if TYPE_CHECKING:
    from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode


class TreeNotePart(Base):
    """One addressable Markdown block in a tree_note_node hierarchy."""

    __tablename__ = 'tree_note_part'
    __table_args__ = (
        ForeignKeyConstraint(
            ['node_id'],
            [TreeNoteDigest.col_ref('tree_note_node.id')],
            ondelete='CASCADE',
            name='fk_tree_note_part_node',
        ),
        ForeignKeyConstraint(
            ['parent_part_id', 'node_id'],
            [TreeNoteDigest.col_ref('tree_note_part.id'), TreeNoteDigest.col_ref('tree_note_part.node_id')],
            ondelete='CASCADE',
            name='fk_tree_note_part_parent_node',
        ),
        UniqueConstraint('id', 'node_id', name='uq_tree_note_part_id_node'),
        CheckConstraint('parent_part_id IS NULL OR id != parent_part_id', name='ck_tree_note_part_not_self_parent'),
        CheckConstraint('position BETWEEN 0 AND 2000000000', name='ck_tree_note_part_position'),
        CheckConstraint(
            "part_kind IN ('section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule')",
            name='ck_tree_note_part_kind',
        ),
        CheckConstraint("part_kind != 'item' OR parent_part_id IS NOT NULL", name='ck_tree_note_part_item_has_parent'),
        CheckConstraint(
            "(part_kind = 'section' AND heading_level BETWEEN 1 AND 6) "
            "OR (part_kind != 'section' AND heading_level IS NULL)",
            name='ck_tree_note_part_heading_level_by_kind',
        ),
        CheckConstraint(
            "(part_kind = 'section' AND title IS NOT NULL AND length(trim(title)) > 0) "
            "OR (part_kind != 'section' AND title IS NULL)",
            name='ck_tree_note_part_title_by_kind',
        ),
        CheckConstraint(
            "(part_kind = 'list' AND list_style IN ('unordered', 'ordered')) "
            "OR (part_kind != 'list' AND list_style IS NULL)",
            name='ck_tree_note_part_list_style_by_kind',
        ),
        CheckConstraint(
            "(part_kind = 'fence' AND (language IS NULL "
            'OR (length(trim(language)) > 0 AND language = trim(language)))) '
            "OR (part_kind != 'fence' AND language IS NULL)",
            name='ck_tree_note_part_language_by_kind',
        ),
        CheckConstraint("part_kind = 'item' OR checked IS NULL", name='ck_tree_note_part_checked_item_only'),
        CheckConstraint(
            "(part_kind IN ('section', 'list', 'rule') AND content_markdown = '') "
            "OR (part_kind IN ('paragraph', 'table') AND length(trim(content_markdown)) > 0) "
            "OR (part_kind IN ('fence', 'item', 'quote'))",
            name='ck_tree_note_part_content_by_kind',
        ),
        CheckConstraint('updated_at >= 0', name='ck_tree_note_part_updated_at'),
        Index(
            'ux_tree_note_part_child_pos',
            'node_id',
            'parent_part_id',
            'position',
            unique=True,
            sqlite_where=text('parent_part_id IS NOT NULL'),
            postgresql_where=text('parent_part_id IS NOT NULL'),
        ),
        Index(
            'ux_tree_note_part_top_pos',
            'node_id',
            'position',
            unique=True,
            sqlite_where=text('parent_part_id IS NULL'),
            postgresql_where=text('parent_part_id IS NULL'),
        ),
        Index('ix_tree_note_part_node_parent_pos', 'node_id', 'parent_part_id', 'position', 'id'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_part_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    part_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    heading_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False, default='')
    list_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    node: Mapped[TreeNoteNode] = relationship(
        'TreeNoteNode',
        back_populates='parts',
        foreign_keys=[node_id],
        passive_deletes=True,
    )
    parent: Mapped[TreeNotePart | None] = relationship(
        'TreeNotePart',
        back_populates='children',
        primaryjoin='TreeNotePart.id == foreign(TreeNotePart.parent_part_id)',
        remote_side='[TreeNotePart.id]',
        overlaps='node',
    )
    children: Mapped[list[TreeNotePart]] = relationship(
        'TreeNotePart',
        back_populates='parent',
        primaryjoin='TreeNotePart.id == foreign(TreeNotePart.parent_part_id)',
        order_by='(TreeNotePart.position, TreeNotePart.id)',
        cascade='all, delete-orphan',
        passive_deletes=True,
        overlaps='node',
    )
