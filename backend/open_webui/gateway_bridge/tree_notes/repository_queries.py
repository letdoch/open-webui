"""Read-only queries and hierarchical projections for tree notes, daily calendar, and search."""

from __future__ import annotations

import calendar as std_calendar
import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.daily_entry_model import TreeNoteDailyEntry
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.external_ref_model import TreeNoteExternalRef
from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.tag_model import TreeNoteTag
from open_webui.internal.db import get_async_db


class TreeNoteQueries:
    """Owner-scoped read queries and tree/calendar/search projections."""

    class TreeQueryParams(BaseModel):
        """Query options for loading the hierarchical workspace tree."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        include_archived: bool = False
        include_daily: bool = False
        root_id: str | None = None

    class CalendarQueryParams(BaseModel):
        """Query options for projecting the YYYY / Month / DD daily calendar tree."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        year: int | None = None
        month: int | None = None
        from_date: str | None = None
        to_date: str | None = None
        include_archived: bool = False

    class SearchQueryParams(BaseModel):
        """Query options for owner-scoped node and tag search."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        query: str = ''
        tag: str | None = None
        node_kind: Literal['folder', 'note', 'daily_note', 'task_draft'] | None = None
        include_archived: bool = False
        limit: int = 20
        offset: int = 0

    class _ForestBundle(BaseModel):
        """Precomputed in-memory forest lookup tables for tree/calendar assembly."""

        model_config = ConfigDict(strict=True, arbitrary_types_allowed=True, extra='forbid')

        children_map: dict[str | None, list[TreeNoteNode]]
        child_counts: dict[str, int]
        tags_map: dict[str, list[str]]
        daily_map: dict[str, str]

    class _SearchBundle(BaseModel):
        """Precomputed lookup bundle for evaluating owner search matches."""

        model_config = ConfigDict(strict=True, arbitrary_types_allowed=True, extra='forbid')

        parts_by_node: dict[str, list[TreeNotePart]]
        node_by_id: dict[str, TreeNoteNode]
        tags_map: dict[str, list[str]]
        daily_map: dict[str, str]
        children_count: dict[str, int]
        needle: str

    @classmethod
    async def get_tree(
        cls,
        owner_user_id: str,
        params: TreeNoteQueries.TreeQueryParams,
    ) -> TreeNoteContracts.TreeResponse | None:
        """Load ordered tree nodes for ``owner_user_id``, returning ``None`` if ``root_id`` is missing.

        Args:
            owner_user_id: Authenticated owner user ID.
            params: Tree filtering options.

        Returns:
            Hierarchical ``TreeResponse``, or ``None`` if ``root_id`` does not exist for owner.
        """
        async with get_async_db() as session:
            nodes, tags_map, daily_map = await cls._load_owner_forest(session, owner_user_id)
            if params.root_id is not None and not any(n.id == params.root_id for n in nodes):
                return None
            filtered = [n for n in nodes if params.include_archived or n.archived_at is None]
            if params.root_id is not None and not any(n.id == params.root_id for n in filtered):
                return None
            children_map, child_counts = cls._build_children_index(filtered)
            forest = cls._ForestBundle(
                children_map=children_map,
                child_counts=child_counts,
                tags_map=tags_map,
                daily_map=daily_map,
            )
            if params.root_id is not None:
                root_node = next(n for n in filtered if n.id == params.root_id)
                items = [cls._build_tree_item(root_node, forest)]
                return TreeNoteContracts.TreeResponse(items=items, total_nodes=cls._count_items(items))
            roots = [
                n
                for n in (children_map[None] if None in children_map else [])
                if params.include_daily or n.node_kind != 'daily_note'
            ]
            items = [cls._build_tree_item(root, forest) for root in roots]
            return TreeNoteContracts.TreeResponse(items=items, total_nodes=cls._count_items(items))

    @classmethod
    async def get_calendar(
        cls,
        owner_user_id: str,
        params: TreeNoteQueries.CalendarQueryParams,
    ) -> TreeNoteContracts.CalendarResponse:
        """Project daily entries into ``Year -> Month -> Day -> child subtrees``.

        Args:
            owner_user_id: Authenticated owner user ID.
            params: Calendar range and archive filter options.

        Returns:
            Projected ``CalendarResponse`` without persisted calendar hierarchy columns.
        """
        async with get_async_db() as session:
            nodes, tags_map, daily_map = await cls._load_owner_forest(session, owner_user_id)
            filtered = [n for n in nodes if params.include_archived or n.archived_at is None]
            children_map, child_counts = cls._build_children_index(filtered)
            forest = cls._ForestBundle(
                children_map=children_map,
                child_counts=child_counts,
                tags_map=tags_map,
                daily_map=daily_map,
            )
            return cls._assemble_calendar(filtered, forest, params)

    @classmethod
    def _assemble_calendar(
        cls,
        filtered: list[TreeNoteNode],
        forest: TreeNoteQueries._ForestBundle,
        params: TreeNoteQueries.CalendarQueryParams,
    ) -> TreeNoteContracts.CalendarResponse:
        """Group daily nodes into sorted Year -> Month -> Day hierarchies.

        Args:
            filtered: Archive-filtered owner ``TreeNoteNode`` list.
            forest: Precomputed children/tag/daily lookup bundle.
            params: Calendar range filter options.

        Returns:
            Projected ``CalendarResponse``.
        """
        daily_nodes = sorted(
            [
                (forest.daily_map[n.id], n)
                for n in filtered
                if (
                    n.node_kind == 'daily_note'
                    and n.id in forest.daily_map
                    and cls._matches_calendar_filter(forest.daily_map[n.id], params)
                )
            ],
            key=lambda pair: pair[0],
            reverse=True,
        )
        years_map: dict[int, dict[int, list[TreeNoteContracts.CalendarDayEntry]]] = {}
        for note_date, node in daily_nodes:
            parsed_date = dt.date.fromisoformat(note_date)
            summary = cls._node_summary(node, forest.tags_map, forest.child_counts, note_date)
            sub_children = [
                cls._build_tree_item(child, forest)
                for child in (forest.children_map[node.id] if node.id in forest.children_map else [])
            ]
            day_entry = TreeNoteContracts.CalendarDayEntry(
                note_date=note_date,
                node=summary,
                children=sub_children,
            )
            if parsed_date.year not in years_map:
                empty_month_bucket: dict[int, list[TreeNoteContracts.CalendarDayEntry]] = {}
                years_map[parsed_date.year] = empty_month_bucket
            month_bucket = years_map[parsed_date.year]
            if parsed_date.month not in month_bucket:
                month_bucket[parsed_date.month] = []
            month_bucket[parsed_date.month].append(day_entry)

        year_groups = [
            TreeNoteContracts.CalendarYearGroup(
                year=year,
                months=[
                    TreeNoteContracts.CalendarMonthGroup(
                        month=month,
                        month_label=std_calendar.month_name[month],
                        days=years_map[year][month],
                    )
                    for month in sorted(years_map[year].keys(), reverse=True)
                ],
            )
            for year in sorted(years_map.keys(), reverse=True)
        ]
        return TreeNoteContracts.CalendarResponse(years=year_groups, total_days=len(daily_nodes))

    @classmethod
    async def search(
        cls,
        owner_user_id: str,
        params: TreeNoteQueries.SearchQueryParams,
    ) -> TreeNoteContracts.SearchResponse:
        """Search owner-scoped tree notes across titles, Markdown parts, and tag slugs.

        Args:
            owner_user_id: Authenticated owner user ID.
            params: Search query, tag, kind, and pagination options.

        Returns:
            Paginated ``SearchResponse`` ordered by match relevance and ``updated_at DESC``.
        """
        async with get_async_db() as session:
            nodes, tags_map, daily_map = await cls._load_owner_forest(session, owner_user_id)
            parts_by_node = await cls._load_parts_by_node(session, [n.id for n in nodes])
            bundle = cls._SearchBundle(
                parts_by_node=parts_by_node,
                node_by_id={n.id: n for n in nodes},
                tags_map=tags_map,
                daily_map=daily_map,
                children_count=cls._count_direct_children(nodes, params.include_archived),
                needle=TreeNoteDigest.normalize_text(params.query).strip().casefold(),
            )
            return cls._rank_search_hits(nodes, bundle, params)

    @classmethod
    def _rank_search_hits(
        cls,
        nodes: list[TreeNoteNode],
        bundle: TreeNoteQueries._SearchBundle,
        params: TreeNoteQueries.SearchQueryParams,
    ) -> TreeNoteContracts.SearchResponse:
        """Score, filter, and paginate matching search hits in memory.

        Args:
            nodes: Owner-scoped node list.
            bundle: Precomputed parts/tags/daily/needle lookup bundle.
            params: Filter and pagination options.

        Returns:
            Paginated ``SearchResponse``.
        """
        clean_tag = TreeNoteDigest.normalize_tag_slug(params.tag) if params.tag is not None else None
        hits: list[tuple[int, int, TreeNoteContracts.SearchHit]] = []
        for node in nodes:
            if not cls._passes_search_filters(node, bundle.tags_map, params, clean_tag):
                continue
            hit = cls._match_node(node, bundle)
            if hit is not None:
                score, item = hit
                hits.append((score, node.updated_at, item))
        hits.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        sliced = [entry[2] for entry in hits[params.offset:params.offset + params.limit]]
        return TreeNoteContracts.SearchResponse(items=sliced, total=len(hits))

    @classmethod
    async def read_node(
        cls,
        owner_user_id: str,
        node_id: str,
        include_parts: bool = True,
    ) -> TreeNotePartContracts.ReadResponse | None:
        """Read a single owned node with computed digests, rendered Markdown, and breadcrumbs.

        Args:
            owner_user_id: Authenticated owner user ID.
            node_id: Target node identifier.
            include_parts: Whether to include detailed ``PartDTO`` entries in response.

        Returns:
            Complete ``ReadResponse``, or ``None`` if not found or not owned by user.
        """
        async with get_async_db() as session:
            return await cls.build_read_response(session, owner_user_id, node_id, include_parts=include_parts)

    @classmethod
    async def build_read_response(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        node_id: str,
        include_parts: bool = True,
    ) -> TreeNotePartContracts.ReadResponse | None:
        """Assemble ``ReadResponse`` inside an existing ``AsyncSession`` for one owned node.

        Args:
            session: Active SQLAlchemy ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            include_parts: Whether to populate the ``parts`` list.

        Returns:
            ``ReadResponse`` or ``None`` when missing or foreign.
        """
        node = (
            await session.execute(
                select(TreeNoteNode).where(TreeNoteNode.id == node_id, TreeNoteNode.owner_user_id == owner_user_id)
            )
        ).scalar_one_or_none()
        if node is None:
            return None

        parts = await cls._load_node_parts(session, node.id)
        tags, daily_entry, refs = await cls._load_node_relations(session, owner_user_id, node.id)
        breadcrumbs = await cls._load_breadcrumbs(session, owner_user_id, node)
        child_summaries = await cls._load_child_summaries(session, owner_user_id, node_id)
        basis_sha256, part_digests = TreeNoteDigest.compute_node_digest(node, parts)
        part_paths = TreeNoteDigest.compute_part_paths(parts)
        return TreeNotePartContracts.ReadResponse(
            id=node.id,
            parent_id=node.parent_id,
            sort_order=node.sort_order,
            title=node.title,
            node_kind=TreeNoteDigest.narrow_node_kind(node.node_kind),
            note_date=daily_entry.note_date if daily_entry is not None else None,
            archived_at=node.archived_at,
            created_at=node.created_at,
            updated_at=node.updated_at,
            basis_sha256=basis_sha256,
            rendered_markdown=TreeNotePartsCodec.render_markdown(parts),
            tags=tags,
            breadcrumbs=breadcrumbs,
            parts=[cls.part_to_dto(part, part_digests[part.id], part_paths[part.id]) for part in parts]
            if include_parts
            else [],
            external_refs=[
                TreeNotePartContracts.ExternalRefDTO(
                    id=ref.id,
                    target_kind=TreeNoteDigest.narrow_target_kind(ref.target_kind),
                    external_id=ref.external_id,
                    created_at=ref.created_at,
                )
                for ref in refs
            ],
            children=child_summaries,
        )

    @staticmethod
    async def _load_node_parts(session: AsyncSession, node_id: str) -> list[TreeNotePart]:
        """Load ordered parts for a single node.

        Args:
            session: Active SQLAlchemy ``AsyncSession``.
            node_id: Owning node identifier.

        Returns:
            Ordered list of ``TreeNotePart`` rows.
        """
        return list(
            (
                await session.execute(
                    select(TreeNotePart)
                    .where(TreeNotePart.node_id == node_id)
                    .order_by(TreeNotePart.position.asc(), TreeNotePart.id.asc())
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def _load_node_relations(
        session: AsyncSession,
        owner_user_id: str,
        node_id: str,
    ) -> tuple[list[str], TreeNoteDailyEntry | None, list[TreeNoteExternalRef]]:
        """Load tags, daily calendar subtype row, and external refs for one node.

        Args:
            session: Active SQLAlchemy ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.

        Returns:
            Tuple of ``(tag_slugs, daily_entry_or_none, external_refs)``.
        """
        raw_tags = (
            await session.execute(
                select(TreeNoteTag.tag_slug)
                .where(TreeNoteTag.node_id == node_id)
                .order_by(TreeNoteTag.tag_slug.asc())
            )
        ).scalars().all()
        daily_entry = (
            await session.execute(select(TreeNoteDailyEntry).where(TreeNoteDailyEntry.node_id == node_id))
        ).scalar_one_or_none()
        refs = list(
            (
                await session.execute(
                    select(TreeNoteExternalRef)
                    .where(TreeNoteExternalRef.node_id == node_id, TreeNoteExternalRef.owner_user_id == owner_user_id)
                    .order_by(TreeNoteExternalRef.created_at.asc(), TreeNoteExternalRef.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [str(t) for t in raw_tags], daily_entry, refs

    @staticmethod
    def part_to_dto(part: TreeNotePart, digest: str, part_path: str) -> TreeNotePartContracts.PartDTO:
        """Convert one ``TreeNotePart`` row and derived digest/path into ``PartDTO``.

        Args:
            part: Loaded ``TreeNotePart`` ORM instance.
            digest: Computed SHA-256 ``part_basis_sha256``.
            part_path: Computed dotted ``part_path``.

        Returns:
            Strict ``PartDTO`` instance.
        """
        return TreeNotePartContracts.PartDTO(
            id=part.id,
            node_id=part.node_id,
            parent_part_id=part.parent_part_id,
            position=part.position,
            part_kind=TreeNoteDigest.narrow_part_kind(part.part_kind),
            heading_level=part.heading_level,
            title=part.title,
            content_markdown=TreeNoteDigest.normalize_text(part.content_markdown),
            list_style=TreeNoteDigest.narrow_list_style(part.list_style),
            language=part.language,
            checked=TreeNoteDigest.coerce_checked(part.checked),
            updated_at=part.updated_at,
            part_basis_sha256=digest,
            part_path=part_path,
        )

    @staticmethod
    async def _load_owner_forest(
        session: AsyncSession,
        owner_user_id: str,
    ) -> tuple[list[TreeNoteNode], dict[str, list[str]], dict[str, str]]:
        """Load all nodes, tag lists, and daily dates for one owner.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.

        Returns:
            Tuple of ``(ordered_nodes, tags_by_node_id, note_date_by_node_id)``.
        """
        nodes = list(
            (
                await session.execute(
                    select(TreeNoteNode)
                    .where(TreeNoteNode.owner_user_id == owner_user_id)
                    .order_by(TreeNoteNode.sort_order.asc(), TreeNoteNode.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not nodes:
            return [], {}, {}
        tags_rows = (
            await session.execute(
                select(TreeNoteTag.node_id, TreeNoteTag.tag_slug)
                .join(TreeNoteNode, TreeNoteTag.node_id == TreeNoteNode.id)
                .where(TreeNoteNode.owner_user_id == owner_user_id)
                .order_by(TreeNoteTag.tag_slug.asc())
            )
        ).all()
        tags_map: dict[str, list[str]] = {}
        for nid, slug in tags_rows:
            tags_map.setdefault(str(nid), []).append(str(slug))
        daily_rows = (
            await session.execute(
                select(TreeNoteDailyEntry.node_id, TreeNoteDailyEntry.note_date).where(
                    TreeNoteDailyEntry.owner_user_id == owner_user_id
                )
            )
        ).all()
        daily_map = {str(nid): str(ndate) for nid, ndate in daily_rows}
        return nodes, tags_map, daily_map

    @staticmethod
    async def _load_parts_by_node(session: AsyncSession, node_ids: list[str]) -> dict[str, list[TreeNotePart]]:
        """Batch-load ordered parts for a list of owned node IDs.

        Args:
            session: Active ``AsyncSession``.
            node_ids: List of owner node UUIDs to batch-load.

        Returns:
            Mapping from ``node_id`` to ordered ``TreeNotePart`` list.
        """
        grouped: dict[str, list[TreeNotePart]] = {}
        for idx in range(0, len(node_ids), 500):
            chunk = node_ids[idx:idx + 500]
            rows = (
                await session.execute(
                    select(TreeNotePart)
                    .where(TreeNotePart.node_id.in_(chunk))
                    .order_by(TreeNotePart.position.asc(), TreeNotePart.id.asc())
                )
            ).scalars().all()
            for part in rows:
                grouped.setdefault(part.node_id, []).append(part)
        return grouped

    @staticmethod
    def _build_children_index(nodes: list[TreeNoteNode]) -> tuple[dict[str | None, list[TreeNoteNode]], dict[str, int]]:
        """Group ordered children by parent_id and compute child counts.

        Args:
            nodes: Flat list of owner ``TreeNoteNode`` rows.

        Returns:
            Tuple of ``(children_by_parent_id, child_counts_by_node_id)``.
        """
        children_map: dict[str | None, list[TreeNoteNode]] = {}
        for node in sorted(nodes, key=lambda item: (item.sort_order, item.id)):
            children_map.setdefault(node.parent_id, []).append(node)
        counts = {parent_id: len(items) for parent_id, items in children_map.items() if parent_id is not None}
        return children_map, counts

    @classmethod
    def _build_tree_item(
        cls,
        node: TreeNoteNode,
        forest: TreeNoteQueries._ForestBundle,
    ) -> TreeNoteContracts.TreeNodeItem:
        """Build a recursive ``TreeNodeItem`` projection.

        Args:
            node: Current subtree root ``TreeNoteNode``.
            forest: Precomputed children/tags/daily lookup bundle.

        Returns:
            Recursive ``TreeNoteItem``.
        """
        summary = cls._node_summary(node, forest.tags_map, forest.child_counts, forest.daily_map.get(node.id))
        child_nodes = forest.children_map[node.id] if node.id in forest.children_map else []
        children = [cls._build_tree_item(child, forest) for child in child_nodes]
        return TreeNoteContracts.TreeNodeItem(node=summary, children=children)

    @classmethod
    def _count_items(cls, items: list[TreeNoteContracts.TreeNodeItem]) -> int:
        """Count total recursive nodes inside a tree projection.

        Args:
            items: Recursive ``TreeNodeItem`` list.

        Returns:
            Total count of nodes across the subtree.
        """
        return sum(1 + cls._count_items(item.children) for item in items)

    @staticmethod
    def _node_summary(
        node: TreeNoteNode,
        tags_map: dict[str, list[str]],
        child_counts: dict[str, int],
        note_date: str | None,
    ) -> TreeNoteContracts.NodeSummaryDTO:
        """Create a compact ``NodeSummaryDTO`` for a node.

        Args:
            node: Target ``TreeNoteNode`` ORM row.
            tags_map: Mapping from ``node_id`` to normalized tag slugs.
            child_counts: Mapping from ``node_id`` to child count.
            note_date: Optional calendar ``YYYY-MM-DD`` date for daily nodes.

        Returns:
            Populated ``NodeSummaryDTO``.
        """
        return TreeNoteContracts.NodeSummaryDTO(
            id=node.id,
            parent_id=node.parent_id,
            sort_order=node.sort_order,
            title=node.title,
            node_kind=TreeNoteDigest.narrow_node_kind(node.node_kind),
            note_date=note_date,
            archived_at=node.archived_at,
            created_at=node.created_at,
            updated_at=node.updated_at,
            tags=tags_map[node.id] if node.id in tags_map else [],
            child_count=child_counts[node.id] if node.id in child_counts else 0,
        )

    @staticmethod
    def _matches_calendar_filter(note_date: str, params: TreeNoteQueries.CalendarQueryParams) -> bool:
        """Check whether an ISO calendar date satisfies optional year/month/range filters.

        Args:
            note_date: Canonical ISO-8601 date ``YYYY-MM-DD``.
            params: Requested calendar filter bounds.

        Returns:
            True if ``note_date`` satisfies every configured filter.
        """
        parsed = dt.date.fromisoformat(note_date)
        if params.year is not None and parsed.year != params.year:
            return False
        if params.month is not None and parsed.month != params.month:
            return False
        if params.from_date is not None and note_date < params.from_date:
            return False
        if params.to_date is not None and note_date > params.to_date:
            return False
        return True

    @staticmethod
    def _count_direct_children(nodes: list[TreeNoteNode], include_archived: bool) -> dict[str, int]:
        """Count non-archived (or all) direct children per node ID.

        Args:
            nodes: Owner-scoped node list.
            include_archived: Whether archived children should be counted.

        Returns:
            Mapping from ``parent_id`` to visible direct child count.
        """
        counts: dict[str, int] = {}
        for node in nodes:
            if not include_archived and node.archived_at is not None:
                continue
            if node.parent_id is not None:
                counts[node.parent_id] = (counts[node.parent_id] if node.parent_id in counts else 0) + 1
        return counts

    @staticmethod
    def _passes_search_filters(
        node: TreeNoteNode,
        tags_map: dict[str, list[str]],
        params: TreeNoteQueries.SearchQueryParams,
        clean_tag: str | None,
    ) -> bool:
        """Apply archive, node_kind, and tag filters to a candidate search node.

        Args:
            node: Candidate ``TreeNoteNode``.
            tags_map: Mapping from ``node_id`` to normalized tag slugs.
            params: Search filter parameters.
            clean_tag: Optional normalized tag filter slug.

        Returns:
            True if ``node`` satisfies all filter predicates.
        """
        if not params.include_archived and node.archived_at is not None:
            return False
        if params.node_kind is not None and node.node_kind != params.node_kind:
            return False
        if clean_tag is not None and clean_tag not in (tags_map[node.id] if node.id in tags_map else []):
            return False
        return True

    @classmethod
    def _match_node(
        cls,
        node: TreeNoteNode,
        bundle: TreeNoteQueries._SearchBundle,
    ) -> tuple[int, TreeNoteContracts.SearchHit] | None:
        """Evaluate query relevance and construct a ``SearchHit`` on match.

        Args:
            node: Candidate ``TreeNoteNode``.
            bundle: Precomputed search lookup bundle.

        Returns:
            Tuple of ``(score, SearchHit)`` if matched, otherwise ``None``.
        """
        parts = bundle.parts_by_node[node.id] if node.id in bundle.parts_by_node else []
        node_tags = bundle.tags_map[node.id] if node.id in bundle.tags_map else []
        matched_part: TreeNotePart | None = None
        score = 0
        if not bundle.needle:
            score = 1
        else:
            if bundle.needle in node.title.casefold():
                score = 3
            if any(bundle.needle in slug for slug in node_tags):
                score = max(score, 2)
            for part in parts:
                haystack = f'{part.title or ""}\n{part.content_markdown}'.casefold()
                if bundle.needle in haystack:
                    matched_part = part
                    score = max(score, 2)
                    break
        if score == 0:
            return None

        part_paths = TreeNoteDigest.compute_part_paths(parts) if matched_part is not None else {}
        snippet = cls._build_snippet(matched_part, parts, bundle.needle)
        breadcrumbs = cls._breadcrumbs_from_map(node, bundle.node_by_id)
        summary = cls._node_summary(node, bundle.tags_map, bundle.children_count, bundle.daily_map.get(node.id))
        hit = TreeNoteContracts.SearchHit(
            node=summary,
            snippet=snippet,
            matched_part_id=matched_part.id if matched_part is not None else None,
            matched_part_path=part_paths[matched_part.id] if matched_part is not None else None,
            breadcrumbs=breadcrumbs,
        )
        return score, hit

    @staticmethod
    def _build_snippet(matched_part: TreeNotePart | None, parts: list[TreeNotePart], needle: str) -> str:
        """Extract a short readable snippet centered on the query match.

        Args:
            matched_part: First matching ``TreeNotePart`` if content matched.
            parts: Ordered list of parts belonging to the node.
            needle: Normalized casefolded search needle.

        Returns:
            Truncated single-line excerpt suitable for search results.
        """
        source = ''
        if matched_part is not None:
            if matched_part.part_kind == 'section' and matched_part.title:
                source = matched_part.title
            else:
                source = matched_part.content_markdown
        elif parts:
            source = TreeNotePartsCodec.render_markdown(parts[:3])
        compact = ' '.join(source.split())
        if not compact:
            return ''
        if not needle:
            return compact[:160] + ('...' if len(compact) > 160 else '')
        idx = compact.casefold().find(needle)
        if idx < 0:
            return compact[:160] + ('...' if len(compact) > 160 else '')
        start = max(0, idx - 50)
        end = min(len(compact), idx + len(needle) + 90)
        prefix = '...' if start > 0 else ''
        suffix = '...' if end < len(compact) else ''
        return f'{prefix}{compact[start:end]}{suffix}'

    @staticmethod
    def _breadcrumbs_from_map(
        node: TreeNoteNode,
        node_by_id: dict[str, TreeNoteNode],
    ) -> list[TreeNoteContracts.BreadcrumbItem]:
        """Build root-to-node breadcrumbs from an in-memory node lookup.

        Args:
            node: Target leaf ``TreeNoteNode``.
            node_by_id: In-memory lookup dictionary of owner nodes.

        Returns:
            Root-to-node ordered list of ``BreadcrumbItem`` models.
        """
        trail: list[TreeNoteContracts.BreadcrumbItem] = []
        seen: set[str] = set()
        current: TreeNoteNode | None = node
        while current is not None and current.id not in seen:
            seen.add(current.id)
            trail.append(
                TreeNoteContracts.BreadcrumbItem(
                    id=current.id,
                    title=current.title,
                    node_kind=TreeNoteDigest.narrow_node_kind(current.node_kind),
                )
            )
            current = node_by_id.get(current.parent_id) if current.parent_id is not None else None
        trail.reverse()
        return trail

    @classmethod
    async def _load_breadcrumbs(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        node: TreeNoteNode,
    ) -> list[TreeNoteContracts.BreadcrumbItem]:
        """Walk ancestors upward in DB to build breadcrumbs for one node.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node: Target ``TreeNoteNode``.

        Returns:
            Root-to-node ordered list of ``BreadcrumbItem`` models.
        """
        trail: list[TreeNoteContracts.BreadcrumbItem] = []
        seen: set[str] = set()
        current: TreeNoteNode | None = node
        while current is not None and current.id not in seen and len(trail) <= TreeNoteContracts.MAX_TREE_DEPTH:
            seen.add(current.id)
            trail.append(
                TreeNoteContracts.BreadcrumbItem(
                    id=current.id,
                    title=current.title,
                    node_kind=TreeNoteDigest.narrow_node_kind(current.node_kind),
                )
            )
            if current.parent_id is None:
                break
            current = (
                await session.execute(
                    select(TreeNoteNode).where(
                        TreeNoteNode.id == current.parent_id,
                        TreeNoteNode.owner_user_id == owner_user_id,
                    )
                )
            ).scalar_one_or_none()
        trail.reverse()
        return trail

    @classmethod
    async def _load_child_summaries(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        parent_id: str,
    ) -> list[TreeNoteContracts.NodeSummaryDTO]:
        """Load direct ordered child summaries for a single node.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            parent_id: Parent node identifier.

        Returns:
            Ordered list of direct child ``NodeSummaryDTO`` entries.
        """
        children = list(
            (
                await session.execute(
                    select(TreeNoteNode)
                    .where(TreeNoteNode.owner_user_id == owner_user_id, TreeNoteNode.parent_id == parent_id)
                    .order_by(TreeNoteNode.sort_order.asc(), TreeNoteNode.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not children:
            return []
        child_ids = [child.id for child in children]
        grand_rows = (
            await session.execute(
                select(TreeNoteNode.parent_id)
                .where(TreeNoteNode.owner_user_id == owner_user_id, TreeNoteNode.parent_id.in_(child_ids))
            )
        ).scalars().all()
        counts: dict[str, int] = {}
        for pid in grand_rows:
            if pid is not None:
                counts[pid] = (counts[pid] if pid in counts else 0) + 1
        tag_rows = (
            await session.execute(
                select(TreeNoteTag.node_id, TreeNoteTag.tag_slug)
                .where(TreeNoteTag.node_id.in_(child_ids))
                .order_by(TreeNoteTag.tag_slug.asc())
            )
        ).all()
        tags_map: dict[str, list[str]] = {}
        for nid, slug in tag_rows:
            tags_map.setdefault(str(nid), []).append(str(slug))
        return [cls._node_summary(child, tags_map, counts, None) for child in children]
