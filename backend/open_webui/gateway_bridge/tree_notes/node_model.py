"""SQLAlchemy 2.0 ORM model for hierarchical tree note nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
from open_webui.models.users import User

if TYPE_CHECKING:
    from open_webui.gateway_bridge.tree_notes.daily_entry_model import TreeNoteDailyEntry
    from open_webui.gateway_bridge.tree_notes.external_ref_model import TreeNoteExternalRef
    from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
    from open_webui.gateway_bridge.tree_notes.tag_model import TreeNoteTag


class TreeNoteNode(Base):
    """Hierarchical personal note, folder, daily calendar root, or task draft."""

    __tablename__ = 'tree_note_node'
    __table_args__ = (
        ForeignKeyConstraint(
            ['owner_user_id'],
            [TreeNoteDigest.col_ref('user.id')],
            ondelete='RESTRICT',
            name='fk_tree_note_node_owner',
        ),
        ForeignKeyConstraint(
            ['parent_id', 'owner_user_id'],
            [TreeNoteDigest.col_ref('tree_note_node.id'), TreeNoteDigest.col_ref('tree_note_node.owner_user_id')],
            ondelete='RESTRICT',
            name='fk_tree_note_node_parent_owner',
        ),
        UniqueConstraint('id', 'owner_user_id', name='uq_tree_note_node_id_owner'),
        UniqueConstraint('id', 'owner_user_id', 'node_kind', name='uq_tree_note_node_id_owner_kind'),
        CheckConstraint('parent_id IS NULL OR id != parent_id', name='ck_tree_note_node_not_self_parent'),
        CheckConstraint('sort_order BETWEEN 0 AND 2000000000', name='ck_tree_note_node_sort_order'),
        CheckConstraint('length(trim(title)) > 0', name='ck_tree_note_node_title_nonempty'),
        CheckConstraint("node_kind IN ('folder', 'note', 'daily_note', 'task_draft')", name='ck_tree_note_node_kind'),
        CheckConstraint("node_kind != 'daily_note' OR parent_id IS NULL", name='ck_tree_note_node_daily_root'),
        CheckConstraint('archived_at IS NULL OR archived_at >= 0', name='ck_tree_note_node_archived_at'),
        CheckConstraint('created_at >= 0', name='ck_tree_note_node_created_at'),
        CheckConstraint('updated_at >= created_at', name='ck_tree_note_node_updated_at'),
        Index(
            'ux_tree_note_node_child_order',
            'owner_user_id',
            'parent_id',
            'sort_order',
            unique=True,
            sqlite_where=text('parent_id IS NOT NULL'),
            postgresql_where=text('parent_id IS NOT NULL'),
        ),
        Index(
            'ux_tree_note_node_root_order',
            'owner_user_id',
            'sort_order',
            unique=True,
            sqlite_where=text('parent_id IS NULL'),
            postgresql_where=text('parent_id IS NULL'),
        ),
        Index('ix_tree_note_node_owner_parent', 'owner_user_id', 'parent_id', 'sort_order', 'id'),
        Index('ix_tree_note_node_owner_archived', 'owner_user_id', 'archived_at', 'updated_at'),
        Index('ix_tree_note_node_owner_kind', 'owner_user_id', 'node_kind'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    node_kind: Mapped[str] = mapped_column(String(16), nullable=False, default='note')
    archived_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    owner: Mapped[User] = relationship('User', foreign_keys=[owner_user_id], lazy='raise')
    parent: Mapped[TreeNoteNode | None] = relationship(
        'TreeNoteNode',
        back_populates='children',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteNode.parent_id)',
        remote_side='[TreeNoteNode.id]',
        overlaps='owner',
    )
    children: Mapped[list[TreeNoteNode]] = relationship(
        'TreeNoteNode',
        back_populates='parent',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteNode.parent_id)',
        order_by='(TreeNoteNode.sort_order, TreeNoteNode.id)',
        passive_deletes=True,
        overlaps='owner',
    )
    daily_entry: Mapped[TreeNoteDailyEntry | None] = relationship(
        'TreeNoteDailyEntry',
        back_populates='node',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteDailyEntry.node_id)',
        uselist=False,
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    parts: Mapped[list[TreeNotePart]] = relationship(
        'TreeNotePart',
        back_populates='node',
        order_by='(TreeNotePart.position, TreeNotePart.id)',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    tags: Mapped[list[TreeNoteTag]] = relationship(
        'TreeNoteTag',
        back_populates='node',
        order_by='TreeNoteTag.tag_slug',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    external_refs: Mapped[list[TreeNoteExternalRef]] = relationship(
        'TreeNoteExternalRef',
        back_populates='node',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteExternalRef.node_id)',
        order_by='(TreeNoteExternalRef.created_at, TreeNoteExternalRef.id)',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
