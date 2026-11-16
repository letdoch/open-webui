"""SQLAlchemy 2.0 ORM model for external promotion and legacy import links."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
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


class TreeNoteExternalRef(Base):
    """Server-owned legacy import anchor or verified downstream task/idea link."""

    __tablename__ = 'tree_note_external_ref'
    __table_args__ = (
        ForeignKeyConstraint(
            ['node_id', 'owner_user_id'],
            [TreeNoteDigest.col_ref('tree_note_node.id'), TreeNoteDigest.col_ref('tree_note_node.owner_user_id')],
            ondelete='CASCADE',
            name='fk_tree_note_external_ref_node_owner',
        ),
        UniqueConstraint('node_id', 'target_kind', 'external_id', name='uq_tree_note_external_ref_node_kind_ext'),
        CheckConstraint(
            "target_kind IN ('project_idea', 'project_task', 'imported_note')",
            name='ck_tree_note_external_ref_target_kind',
        ),
        CheckConstraint('length(trim(external_id)) > 0', name='ck_tree_note_external_ref_external_id'),
        CheckConstraint('created_at >= 0', name='ck_tree_note_external_ref_created_at'),
        Index(
            'ux_tree_note_external_ref_imported_ext',
            'owner_user_id',
            'target_kind',
            'external_id',
            unique=True,
            sqlite_where=text("target_kind = 'imported_note'"),
            postgresql_where=text("target_kind = 'imported_note'"),
        ),
        Index(
            'ux_tree_note_external_ref_imported_node',
            'node_id',
            unique=True,
            sqlite_where=text("target_kind = 'imported_note'"),
            postgresql_where=text("target_kind = 'imported_note'"),
        ),
        Index('ix_tree_note_external_ref_lookup', 'owner_user_id', 'target_kind', 'external_id'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    node: Mapped[TreeNoteNode] = relationship(
        'TreeNoteNode',
        back_populates='external_refs',
        primaryjoin='TreeNoteNode.id == foreign(TreeNoteExternalRef.node_id)',
        passive_deletes=True,
    )
