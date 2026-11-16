"""Shared SQLAlchemy CTE, ordering, and dependent deletion helpers for tree notes."""

from __future__ import annotations

from sqlalchemy import delete, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.daily_entry_model import TreeNoteDailyEntry
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.external_ref_model import TreeNoteExternalRef
from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.tag_model import TreeNoteTag


class TreeNoteTreeHelpers:
    """Stateless async DB helpers shared across tree-note mutation repositories."""

    @staticmethod
    async def load_siblings(session: AsyncSession, owner_user_id: str, parent_id: str | None) -> list[TreeNoteNode]:
        """Load ordered siblings under ``parent_id``.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            parent_id: Parent node ID or ``None`` for root nodes.

        Returns:
            Ordered list of sibling ``TreeNoteNode`` rows.
        """
        parent_cond = TreeNoteNode.parent_id.is_(None) if parent_id is None else TreeNoteNode.parent_id == parent_id
        return list(
            (
                await session.execute(
                    select(TreeNoteNode)
                    .where(TreeNoteNode.owner_user_id == owner_user_id, parent_cond)
                    .order_by(TreeNoteNode.sort_order.asc(), TreeNoteNode.id.asc())
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def walk_ancestors(session: AsyncSession, owner_user_id: str, start_id: str) -> list[str]:
        """Return ordered ancestor IDs starting at ``start_id`` via read-only WITH RECURSIVE.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            start_id: Starting node ID.

        Returns:
            Ordered list of ancestor node IDs from ``start_id`` upward.
        """
        base = (
            select(
                TreeNoteNode.id.label('id'),
                TreeNoteNode.parent_id.label('parent_id'),
                literal(1).label('depth'),
            )
            .where(TreeNoteNode.owner_user_id == owner_user_id, TreeNoteNode.id == start_id)
            .cte(name='ancestors', recursive=True)
        )
        step = select(
            TreeNoteNode.id.label('id'),
            TreeNoteNode.parent_id.label('parent_id'),
            (base.c.depth + 1).label('depth'),
        ).where(
            TreeNoteNode.owner_user_id == owner_user_id,
            TreeNoteNode.id == base.c.parent_id,
            base.c.depth < TreeNoteContracts.MAX_TREE_DEPTH + 2,
        )
        cte = base.union_all(step)
        rows = (await session.execute(select(cte.c.id).order_by(cte.c.depth.asc()))).scalars().all()
        return [str(nid) for nid in rows]

    @staticmethod
    async def collect_subtree_levels(session: AsyncSession, owner_user_id: str, root_id: str) -> list[list[str]]:
        """Collect subtree node IDs ordered deepest-level-first using read-only CTE.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            root_id: Subtree root node ID.

        Returns:
            List of node-ID lists ordered deepest level first.
        """
        base = (
            select(TreeNoteNode.id.label('id'), literal(0).label('depth'))
            .where(TreeNoteNode.owner_user_id == owner_user_id, TreeNoteNode.id == root_id)
            .cte(name='subtree', recursive=True)
        )
        step = select(
            TreeNoteNode.id.label('id'),
            (base.c.depth + 1).label('depth'),
        ).where(
            TreeNoteNode.owner_user_id == owner_user_id,
            TreeNoteNode.parent_id == base.c.id,
            base.c.depth < TreeNoteContracts.MAX_TREE_DEPTH + 2,
        )
        cte = base.union_all(step)
        rows = (await session.execute(select(cte.c.id, cte.c.depth).order_by(cte.c.depth.desc()))).all()
        grouped: dict[int, list[str]] = {}
        for nid, depth in rows:
            grouped.setdefault(int(depth), []).append(str(nid))
        return [grouped[d] for d in sorted(grouped.keys(), reverse=True)]

    @staticmethod
    async def two_phase_node_reorder(session: AsyncSession, ordered_rows: list[TreeNoteNode]) -> None:
        """Apply disjoint positive-offset two-phase sort_order rewrite on sibling nodes.

        Args:
            session: Active ``AsyncSession``.
            ordered_rows: Sibling ``TreeNoteNode`` rows in target order.
        """
        if not ordered_rows:
            return
        offset = max(row.sort_order for row in ordered_rows) + 1
        for idx, row in enumerate(ordered_rows):
            row.sort_order = offset + idx
        await session.flush()
        for idx, row in enumerate(ordered_rows):
            row.sort_order = idx
        await session.flush()

    @staticmethod
    def insert_parsed_parts(session: AsyncSession, node_id: str, markdown: str, updated_at: int) -> None:
        """Insert parsed Markdown parts for a newly created node.

        Args:
            session: Active ``AsyncSession``.
            node_id: Owning node ID.
            markdown: Initial Markdown text.
            updated_at: Nanosecond timestamp applied to inserted parts.
        """
        parsed = TreeNotePartsCodec.parse_markdown(markdown)
        temp_to_id: dict[str, str] = {}
        for part in parsed:
            real_id = TreeNoteDigest.new_uuid7()
            temp_to_id[part.temp_id] = real_id
            parent_id = temp_to_id[part.parent_temp_id] if part.parent_temp_id is not None else None
            session.add(
                TreeNotePart(
                    id=real_id,
                    node_id=node_id,
                    parent_part_id=parent_id,
                    position=part.position,
                    part_kind=part.part_kind,
                    heading_level=part.heading_level,
                    title=part.title,
                    content_markdown=part.content_markdown,
                    list_style=part.list_style,
                    language=part.language,
                    checked=part.checked,
                    updated_at=updated_at,
                )
            )

    @classmethod
    async def delete_node_dependents(cls, session: AsyncSession, node_ids: list[str]) -> None:
        """Explicitly delete external_refs, tags, parts, and daily_entries for SQLite/PG safety.

        Args:
            session: Active ``AsyncSession``.
            node_ids: Node identifiers whose child dependent rows are being deleted.
        """
        for chunk in cls.chunks(node_ids):
            await session.execute(delete(TreeNoteExternalRef).where(TreeNoteExternalRef.node_id.in_(chunk)))
            await session.execute(delete(TreeNoteTag).where(TreeNoteTag.node_id.in_(chunk)))
            await session.execute(delete(TreeNotePart).where(TreeNotePart.node_id.in_(chunk)))
            await session.execute(delete(TreeNoteDailyEntry).where(TreeNoteDailyEntry.node_id.in_(chunk)))

    @staticmethod
    def depth_levels_bottom_up(parent_map: dict[str, str | None]) -> list[list[str]]:
        """Group node IDs by tree depth ordered deepest-first for RESTRICT-safe deletion.

        Args:
            parent_map: Mapping from ``node_id`` to optional ``parent_id``.

        Returns:
            List of node-ID buckets sorted deepest depth first.
        """
        depths: dict[str, int] = {}

        def resolve_depth(node_id: str, trail: set[str]) -> int:
            """Compute node nesting depth safely with cycle detection.

            Args:
                node_id: Node UUID to measure.
                trail: Active ancestor set for cycle breaking.

            Returns:
                Non-negative tree depth.
            """
            if node_id in depths:
                return depths[node_id]
            parent_id = parent_map.get(node_id)
            if parent_id is None or parent_id not in parent_map or parent_id in trail:
                depths[node_id] = 0
                return 0
            trail.add(node_id)
            val = resolve_depth(parent_id, trail) + 1
            depths[node_id] = val
            return val

        by_depth: dict[int, list[str]] = {}
        for nid in parent_map:
            d = resolve_depth(nid, set())
            by_depth.setdefault(d, []).append(nid)
        return [by_depth[d] for d in sorted(by_depth.keys(), reverse=True)]

    @staticmethod
    def chunks(items: list[str], size: int = 500) -> list[list[str]]:
        """Split a list of IDs into chunks safe for SQLite variable limits.

        Args:
            items: Full list of identifier strings.
            size: Maximum slice length per chunk.

        Returns:
            List of contiguous ID slices.
        """
        return [items[idx:idx + size] for idx in range(0, len(items), size)]
