"""Serialized per-owner transactional repository for tree note nodes, daily entries, tags, and refs."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.env import DATABASE_SCHEMA
from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.daily_entry_model import TreeNoteDailyEntry
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.external_ref_model import TreeNoteExternalRef
from open_webui.gateway_bridge.tree_notes.legacy_importer import TreeNoteLegacyImporter
from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.repository_helpers import TreeNoteTreeHelpers
from open_webui.gateway_bridge.tree_notes.repository_queries import TreeNoteQueries
from open_webui.gateway_bridge.tree_notes.tag_model import TreeNoteTag
from open_webui.internal.db import get_async_db
from open_webui.models.users import User


class TreeNoteRepository:
    """Per-owner serialized transactional mutations for tree note nodes and lifecycle."""

    class RepoError(Exception):
        """Structured repository domain or concurrency error."""

        def __init__(
            self,
            status_code: int,
            error_code: str,
            detail: str,
            node_id: str | None = None,
        ) -> None:
            """Initialize a structured repository failure.

            Args:
                status_code: HTTP status code (``403``, ``404``, ``409``, ``422``).
                error_code: Machine-readable error slug.
                detail: Human-readable explanation.
                node_id: Optional related node ID.
            """
            super().__init__(detail)
            self.status_code = status_code
            self.error_code = error_code
            self.detail = detail
            self.node_id = node_id
            self.conflict: TreeNoteContracts.BasisConflictResponse | None = None

        @classmethod
        def from_conflict(
            cls,
            conflict: TreeNoteContracts.BasisConflictResponse,
            detail: str,
        ) -> TreeNoteRepository.RepoError:
            """Create a 409 conflict error carrying a structured CAS payload.

            Args:
                conflict: Computed basis conflict descriptor.
                detail: Human-readable conflict summary.

            Returns:
                Populated ``RepoError``.
            """
            err = cls(409, 'basis_conflict', detail, conflict.node_id)
            err.conflict = conflict
            return err

    class PromoteSnapshot(BaseModel):
        """Verified pre-promotion snapshot captured under the per-owner write lock."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node_id: str
        title: str
        node_kind: Literal['folder', 'note', 'daily_note', 'task_draft']
        rendered_markdown: str
        tags: list[str]
        basis_sha256: str

    class PromoteRecordInput(BaseModel):
        """Input payload for persisting a verified downstream external reference."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node_id: str
        target_kind: Literal['project_idea', 'project_task']
        external_id: str
        expected_basis_sha256: str

    @classmethod
    async def delete_all_for_user(cls, session: AsyncSession, user_id: str) -> None:
        """Delete every tree note row owned by ``user_id`` inside the caller's transaction.

        Args:
            session: Active caller ``AsyncSession`` shared with ``DELETE FROM "user"``.
            user_id: Target user identifier being deleted.
        """
        conn = await session.connection()
        has_table = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table('tree_note_node', schema=DATABASE_SCHEMA)
        )
        if not has_table:
            return
        rows = (
            await session.execute(
                select(TreeNoteNode.id, TreeNoteNode.parent_id).where(TreeNoteNode.owner_user_id == user_id)
            )
        ).all()
        if not rows:
            return
        parent_map = {str(nid): (str(pid) if pid is not None else None) for nid, pid in rows}
        await cls._delete_node_dependents(session, list(parent_map.keys()))
        for level_ids in cls._depth_levels_bottom_up(parent_map):
            for chunk in cls._chunks(level_ids):
                await session.execute(
                    delete(TreeNoteNode).where(TreeNoteNode.owner_user_id == user_id, TreeNoteNode.id.in_(chunk))
                )
        await session.flush()

    @classmethod
    async def create_node(
        cls,
        owner_user_id: str,
        request: TreeNoteContracts.CreateRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Create a new folder, note, or task_draft node with serialized sort_order.

        Args:
            owner_user_id: Verified owner user ID.
            request: Validated creation payload.

        Returns:
            Full ``ReadResponse`` of the newly created node.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            if request.parent_id is not None:
                parent = await cls.require_owned_node(session, owner_user_id, request.parent_id)
                if parent.archived_at is not None:
                    raise cls.RepoError(409, 'node_archived', 'Cannot create under an archived parent.', parent.id)
                depth = await cls._ancestor_depth(session, owner_user_id, parent.id)
                if depth + 1 > TreeNoteContracts.MAX_TREE_DEPTH:
                    raise cls.RepoError(409, 'max_depth_exceeded', 'Maximum tree depth exceeded.', parent.id)

            sort_order = await cls._next_child_sort_order(session, owner_user_id, request.parent_id)
            now_ns = time.time_ns()
            node = TreeNoteNode(
                id=TreeNoteDigest.new_uuid7(),
                owner_user_id=owner_user_id,
                parent_id=request.parent_id,
                sort_order=sort_order,
                title=request.title,
                node_kind=request.node_kind,
                archived_at=None,
                created_at=now_ns,
                updated_at=now_ns,
            )
            session.add(node)
            await session.flush()
            cls._insert_parsed_parts(session, node.id, request.markdown, now_ns)
            for slug in request.tags:
                session.add(TreeNoteTag(node_id=node.id, tag_slug=slug))
            await session.flush()
            await session.commit()
            return await cls.must_read(session, owner_user_id, node.id)

    @classmethod
    async def get_or_create_daily(
        cls,
        owner_user_id: str,
        note_date: str,
    ) -> TreeNotePartContracts.DailyResponse:
        """Atomically get or create the unique daily calendar root for ``note_date``.

        Args:
            owner_user_id: Verified owner user ID.
            note_date: Canonical ISO-8601 date ``YYYY-MM-DD``.

        Returns:
            ``DailyResponse`` with ``created`` indicator and full node read payload.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            existing = await cls._find_daily_node_id(session, owner_user_id, note_date)
            if existing is not None:
                read = await cls.must_read(session, owner_user_id, existing)
                return TreeNotePartContracts.DailyResponse(created=False, note_date=note_date, node=read)

            created_node_id: str | None = None
            created_flag = False
            try:
                async with session.begin_nested():
                    sort_order = await cls._next_child_sort_order(session, owner_user_id, None)
                    now_ns = time.time_ns()
                    new_id = TreeNoteDigest.new_uuid7()
                    session.add(
                        TreeNoteNode(
                            id=new_id,
                            owner_user_id=owner_user_id,
                            parent_id=None,
                            sort_order=sort_order,
                            title=note_date,
                            node_kind='daily_note',
                            archived_at=None,
                            created_at=now_ns,
                            updated_at=now_ns,
                        )
                    )
                    await session.flush()
                    session.add(
                        TreeNoteDailyEntry(
                            node_id=new_id,
                            owner_user_id=owner_user_id,
                            node_kind='daily_note',
                            note_date=note_date,
                        )
                    )
                    await session.flush()
                    created_node_id = new_id
                    created_flag = True
            except IntegrityError:
                created_node_id = await cls._find_daily_node_id(session, owner_user_id, note_date)
                created_flag = False
                if created_node_id is None:
                    raise

            await session.commit()
            if created_node_id is None:
                raise cls.RepoError(404, 'not_found', 'Daily note entry not found after creation.')
            read = await cls.must_read(session, owner_user_id, created_node_id)
            return TreeNotePartContracts.DailyResponse(created=created_flag, note_date=note_date, node=read)

    @classmethod
    async def update_node(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNoteContracts.UpdateRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Update node title or non-daily node_kind under CAS protection.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            request: Validated metadata patch.

        Returns:
            Updated ``ReadResponse``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            if node.archived_at is not None:
                raise cls.RepoError(409, 'node_archived', 'Cannot edit an archived node.', node.id)
            if node.node_kind == 'daily_note' and request.node_kind is not None:
                raise cls.RepoError(409, 'daily_note_kind_locked', 'Daily note kind cannot be changed.', node.id)
            parts = await cls.load_parts(session, node.id)
            cls.verify_node_cas(node, parts, request.expected_basis_sha256)
            if request.title is not None:
                node.title = request.title
            if request.node_kind is not None:
                node.node_kind = request.node_kind
            node.updated_at = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            await session.commit()
            return await cls.must_read(session, owner_user_id, node.id)

    @classmethod
    async def move_node(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNoteContracts.MoveRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Reparent and optionally reposition a node with cycle and depth verification.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Moving node identifier.
            request: CAS-protected move request.

        Returns:
            Updated ``ReadResponse``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            new_parent: TreeNoteNode | None = None
            if request.new_parent_id is not None:
                new_parent = await cls.require_owned_node(session, owner_user_id, request.new_parent_id)
            if node.node_kind == 'daily_note' and request.new_parent_id is not None:
                raise cls.RepoError(409, 'daily_note_root_locked', 'Daily notes must remain root nodes.', node.id)
            if node.archived_at is not None or (new_parent is not None and new_parent.archived_at is not None):
                raise cls.RepoError(409, 'node_archived', 'Cannot move archived node or into archived parent.', node.id)
            if request.new_parent_id == node.id:
                raise cls.RepoError(409, 'cycle_detected', 'Cannot move a node under itself.', node.id)
            if new_parent is not None:
                await cls._check_cycle_and_depth(session, owner_user_id, node.id, new_parent.id)
            parts = await cls.load_parts(session, node.id)
            cls.verify_node_cas(node, parts, request.expected_basis_sha256)

            await cls._apply_move_order(session, owner_user_id, node, request)
            node.updated_at = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            await session.commit()
            return await cls.must_read(session, owner_user_id, node.id)

    @classmethod
    async def reorder_nodes(
        cls,
        owner_user_id: str,
        request: TreeNoteContracts.ReorderRequest,
    ) -> TreeNoteContracts.TreeResponse:
        """Reorder siblings under ``parent_id`` using two-phase positive-offset updates.

        Args:
            owner_user_id: Verified owner user ID.
            request: Reorder payload with expected sibling sequence.

        Returns:
            Refreshed ``TreeResponse``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            if request.parent_id is not None:
                parent = await cls.require_owned_node(session, owner_user_id, request.parent_id)
                if parent.archived_at is not None:
                    raise cls.RepoError(409, 'node_archived', 'Cannot reorder children of archived node.', parent.id)
            for nid in request.ordered_node_ids:
                await cls.require_owned_node(session, owner_user_id, nid)

            all_siblings = await cls._load_siblings(session, owner_user_id, request.parent_id)
            all_ids = [s.id for s in all_siblings]
            non_daily_ids = [s.id for s in all_siblings if s.node_kind != 'daily_note']
            daily_tail_ids = [s.id for s in all_siblings if s.node_kind == 'daily_note']

            if request.parent_id is None and set(request.ordered_node_ids) == set(non_daily_ids):
                live_expected = non_daily_ids
                final_order_ids = [*request.ordered_node_ids, *daily_tail_ids]
            else:
                live_expected = all_ids
                final_order_ids = request.ordered_node_ids
            if live_expected != request.expected_sibling_ids or set(final_order_ids) != set(all_ids):
                raise cls.RepoError(
                    409,
                    'sibling_conflict',
                    'Sibling node list changed concurrently.',
                    request.parent_id,
                )

            by_id = {s.id: s for s in all_siblings}
            await cls._two_phase_node_reorder(session, [by_id[nid] for nid in final_order_ids])
            await session.commit()
        tree = await TreeNoteQueries.get_tree(
            owner_user_id,
            TreeNoteQueries.TreeQueryParams(root_id=request.parent_id),
        )
        if tree is None:
            raise cls.RepoError(404, 'not_found', 'Parent tree root not found.', request.parent_id)
        return tree

    @classmethod
    async def archive_node(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNoteContracts.ArchiveRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Set or clear ``archived_at`` on a node under CAS protection.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            request: CAS-protected archive toggle payload.

        Returns:
            Updated ``ReadResponse``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            parts = await cls.load_parts(session, node.id)
            cls.verify_node_cas(node, parts, request.expected_basis_sha256)
            now_ns = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            node.archived_at = now_ns if request.archived else None
            node.updated_at = now_ns
            await session.commit()
            return await cls.must_read(session, owner_user_id, node.id)

    @classmethod
    async def delete_node(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNoteContracts.DeleteRequest,
    ) -> TreeNoteContracts.DeleteResponse:
        """Delete a leaf node or explicit recursive subtree bottom-up.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target root node identifier.
            request: CAS digest and ``recursive`` flag.

        Returns:
            ``DeleteResponse`` listing deleted node IDs.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            has_child = (
                await session.execute(
                    select(TreeNoteNode.id)
                    .where(TreeNoteNode.owner_user_id == owner_user_id, TreeNoteNode.parent_id == node.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if has_child is not None and not request.recursive:
                raise cls.RepoError(409, 'children_exist', 'Node has child nodes; pass recursive=true.', node.id)
            parts = await cls.load_parts(session, node.id)
            cls.verify_node_cas(node, parts, request.expected_basis_sha256)
            levels = await cls._collect_subtree_levels(session, owner_user_id, node.id)
            all_ids = [nid for level in levels for nid in level]
            await cls._delete_node_dependents(session, all_ids)
            for level_ids in levels:
                for chunk in cls._chunks(level_ids):
                    await session.execute(
                        delete(TreeNoteNode).where(
                            TreeNoteNode.owner_user_id == owner_user_id,
                            TreeNoteNode.id.in_(chunk),
                        )
                    )
            await session.commit()
            return TreeNoteContracts.DeleteResponse(deleted_node_ids=all_ids)

    @classmethod
    async def put_tags(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNotePartContracts.TagsPutRequest,
    ) -> TreeNotePartContracts.TagsResponse:
        """Replace tag slugs on an owned node outside ``basis_sha256``.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            request: Normalized deduplicated tag slug list.

        Returns:
            Updated ``TagsResponse``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            await session.execute(delete(TreeNoteTag).where(TreeNoteTag.node_id == node.id))
            for slug in request.tag_slugs:
                session.add(TreeNoteTag(node_id=node.id, tag_slug=slug))
            node.updated_at = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            await session.commit()
            return TreeNotePartContracts.TagsResponse(node_id=node.id, tag_slugs=request.tag_slugs)

    @classmethod
    async def capture_promote_snapshot(
        cls,
        owner_user_id: str,
        node_id: str,
        expected_basis_sha256: str,
    ) -> TreeNoteRepository.PromoteSnapshot:
        """Validate ownership, archive guard, and CAS digest prior to downstream promotion.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            expected_basis_sha256: Caller CAS digest.

        Returns:
            Immutable ``PromoteSnapshot`` for outbound promotion request.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, node_id)
            if node.archived_at is not None:
                raise cls.RepoError(409, 'node_archived', 'Cannot promote an archived node.', node.id)
            parts = await cls.load_parts(session, node.id)
            basis_sha256, _ = cls.verify_node_cas(node, parts, expected_basis_sha256)
            tags = list(
                (
                    await session.execute(
                        select(TreeNoteTag.tag_slug)
                        .where(TreeNoteTag.node_id == node.id)
                        .order_by(TreeNoteTag.tag_slug.asc())
                    )
                )
                .scalars()
                .all()
            )
            return cls.PromoteSnapshot(
                node_id=node.id,
                title=node.title,
                node_kind=TreeNoteDigest.narrow_node_kind(node.node_kind),
                rendered_markdown=TreeNotePartsCodec.render_markdown(parts),
                tags=[str(t) for t in tags],
                basis_sha256=basis_sha256,
            )

    @classmethod
    async def record_promoted_ref(
        cls,
        owner_user_id: str,
        data: TreeNoteRepository.PromoteRecordInput,
    ) -> TreeNotePartContracts.ExternalRefDTO:
        """Persist a verified downstream reference row inside a fresh per-owner write lock.

        Args:
            owner_user_id: Verified owner user ID.
            data: Verified downstream promotion reference parameters.

        Returns:
            Persisted or idempotently matched ``ExternalRefDTO``.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            node = await cls.require_owned_node(session, owner_user_id, data.node_id)
            if node.archived_at is not None:
                raise cls.RepoError(409, 'node_archived', 'Cannot record promotion on archived node.', node.id)
            parts = await cls.load_parts(session, node.id)
            cls.verify_node_cas(node, parts, data.expected_basis_sha256)
            existing = (
                await session.execute(
                    select(TreeNoteExternalRef).where(
                        TreeNoteExternalRef.node_id == node.id,
                        TreeNoteExternalRef.target_kind == data.target_kind,
                        TreeNoteExternalRef.external_id == data.external_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = TreeNoteExternalRef(
                    id=TreeNoteDigest.new_uuid7(),
                    node_id=node.id,
                    owner_user_id=owner_user_id,
                    target_kind=data.target_kind,
                    external_id=data.external_id,
                    created_at=time.time_ns(),
                )
                session.add(existing)
                await session.commit()
            return TreeNotePartContracts.ExternalRefDTO(
                id=existing.id,
                target_kind=TreeNoteDigest.narrow_target_kind(existing.target_kind),
                external_id=existing.external_id,
                created_at=existing.created_at,
            )

    @classmethod
    async def delete_external_ref(
        cls,
        owner_user_id: str,
        node_id: str,
        ref_id: str,
    ) -> TreeNotePartContracts.DeleteRefResponse:
        """Delete a user-promotable external ref while protecting ``imported_note`` anchors.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.
            ref_id: External reference identifier.

        Returns:
            ``DeleteRefResponse`` containing the removed ref ID.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            await cls.require_owned_node(session, owner_user_id, node_id)
            ref = (
                await session.execute(
                    select(TreeNoteExternalRef).where(
                        TreeNoteExternalRef.id == ref_id,
                        TreeNoteExternalRef.node_id == node_id,
                        TreeNoteExternalRef.owner_user_id == owner_user_id,
                    )
                )
            ).scalar_one_or_none()
            if ref is None:
                raise cls.RepoError(404, 'not_found', 'External reference not found.', node_id)
            if ref.target_kind == 'imported_note':
                raise cls.RepoError(403, 'imported_ref_locked', 'Import anchors cannot be deleted directly.', node_id)
            await session.execute(delete(TreeNoteExternalRef).where(TreeNoteExternalRef.id == ref.id))
            await session.commit()
            return TreeNotePartContracts.DeleteRefResponse(deleted_ref_id=ref_id)

    @classmethod
    async def import_legacy(
        cls,
        owner_user_id: str,
        request: TreeNotePartContracts.ImportLegacyRequest,
    ) -> TreeNotePartContracts.ImportLegacyResponse:
        """Import owned legacy notes inside a serialized per-owner write transaction.

        Args:
            owner_user_id: Verified owner user ID.
            request: Import options (``note_ids`` filter and ``dry_run``).

        Returns:
            ``ImportLegacyResponse`` with per-note outcomes.
        """
        async with get_async_db() as session:
            await cls.acquire_owner_lock(session, owner_user_id)
            response, missing_ids = await TreeNoteLegacyImporter.import_for_user(session, owner_user_id, request)
            if missing_ids:
                raise cls.RepoError(404, 'not_found', f'Legacy note not found: {missing_ids[0]}')
            if not request.dry_run:
                await session.commit()
            return response

    @classmethod
    async def get_user_timezone(cls, user_id: str) -> str:
        """Read the user's configured IANA timezone or fall back to ``'UTC'``.

        Args:
            user_id: Target user identifier.

        Returns:
            Configured IANA timezone string or ``'UTC'``.
        """
        async with get_async_db() as session:
            tz = (await session.execute(select(User.timezone).where(User.id == user_id))).scalar_one_or_none()
            return tz.strip() if isinstance(tz, str) and tz.strip() else 'UTC'

    @classmethod
    async def acquire_owner_lock(cls, session: AsyncSession, owner_user_id: str) -> None:
        """Acquire SQLite BEGIN IMMEDIATE or PostgreSQL owner row FOR UPDATE before any read.

        Args:
            session: Dedicated non-shared ``AsyncSession``.
            owner_user_id: Verified owner user ID.
        """
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ''
        if dialect_name == 'sqlite':
            await session.execute(text('BEGIN IMMEDIATE'))
        elif dialect_name == 'postgresql':
            locked = (
                await session.execute(select(User.id).where(User.id == owner_user_id).with_for_update())
            ).scalar_one_or_none()
            if locked is None:
                raise cls.RepoError(404, 'not_found', 'User account not found.')
            return
        exists = (await session.execute(select(User.id).where(User.id == owner_user_id))).scalar_one_or_none()
        if exists is None:
            raise cls.RepoError(404, 'not_found', 'User account not found.')

    @classmethod
    async def require_owned_node(cls, session: AsyncSession, owner_user_id: str, node_id: str) -> TreeNoteNode:
        """Return an owned ``TreeNoteNode`` or raise 404.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.

        Returns:
            Loaded ``TreeNoteNode``.
        """
        node = (
            await session.execute(
                select(TreeNoteNode).where(TreeNoteNode.id == node_id, TreeNoteNode.owner_user_id == owner_user_id)
            )
        ).scalar_one_or_none()
        if node is None:
            raise cls.RepoError(404, 'not_found', 'Tree note node not found.', node_id)
        return node

    @staticmethod
    async def load_parts(session: AsyncSession, node_id: str) -> list[TreeNotePart]:
        """Load ordered parts for one node.

        Args:
            session: Active ``AsyncSession``.
            node_id: Owning node identifier.

        Returns:
            Ordered list of ``TreeNotePart`` instances.
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

    @classmethod
    async def must_read(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        node_id: str,
    ) -> TreeNotePartContracts.ReadResponse:
        """Build ``ReadResponse`` after commit and fail closed if missing.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.

        Returns:
            Populated ``ReadResponse``.
        """
        result = await TreeNoteQueries.build_read_response(session, owner_user_id, node_id, include_parts=True)
        if result is None:
            raise cls.RepoError(404, 'not_found', 'Tree note node not found.', node_id)
        return result

    @classmethod
    def verify_node_cas(
        cls,
        node: TreeNoteNode,
        parts: list[TreeNotePart],
        expected_basis_sha256: str,
    ) -> tuple[str, dict[str, str]]:
        """Verify node-level CAS digest and raise structured 409 conflict on mismatch.

        Args:
            node: Loaded ``TreeNoteNode``.
            parts: All ``TreeNotePart`` rows of ``node``.
            expected_basis_sha256: Caller-provided basis digest.

        Returns:
            Tuple of ``(current_basis_sha256, part_digests_by_id)``.
        """
        current_basis, part_digests = TreeNoteDigest.compute_node_digest(node, parts)
        if current_basis != expected_basis_sha256:
            conflict = TreeNoteContracts.BasisConflictResponse(
                node_id=node.id,
                expected_basis_sha256=expected_basis_sha256,
                current_basis_sha256=current_basis,
                current_updated_at=node.updated_at,
            )
            raise cls.RepoError.from_conflict(conflict, 'Node changed concurrently.')
        return current_basis, part_digests

    @staticmethod
    async def _load_siblings(session: AsyncSession, owner_user_id: str, parent_id: str | None) -> list[TreeNoteNode]:
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

    @classmethod
    async def _next_child_sort_order(cls, session: AsyncSession, owner_user_id: str, parent_id: str | None) -> int:
        """Allocate next dense sort_order and check sibling bound.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            parent_id: Parent node ID or ``None`` for root nodes.

        Returns:
            Next available dense integer ``sort_order``.

        Raises:
            RepoError: Sibling count reached ``MAX_SIBLINGS_PER_PARENT``.
        """
        parent_cond = TreeNoteNode.parent_id.is_(None) if parent_id is None else TreeNoteNode.parent_id == parent_id
        count_stmt = select(func.count(TreeNoteNode.id)).where(TreeNoteNode.owner_user_id == owner_user_id, parent_cond)
        sibling_count = int((await session.execute(count_stmt)).scalar_one())
        if sibling_count >= TreeNoteContracts.MAX_SIBLINGS_PER_PARENT:
            raise cls.RepoError(409, 'max_siblings_exceeded', 'Sibling node limit exceeded.', parent_id)
        max_stmt = select(func.coalesce(func.max(TreeNoteNode.sort_order), -1)).where(
            TreeNoteNode.owner_user_id == owner_user_id,
            parent_cond,
        )
        return int((await session.execute(max_stmt)).scalar_one()) + 1

    @staticmethod
    async def _find_daily_node_id(session: AsyncSession, owner_user_id: str, note_date: str) -> str | None:
        """Return existing daily root node ID for ``(owner_user_id, note_date)`` if any.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            note_date: Canonical ISO-8601 date ``YYYY-MM-DD``.

        Returns:
            Existing daily root ``node_id`` or ``None``.
        """
        stmt = select(TreeNoteDailyEntry.node_id).where(
            TreeNoteDailyEntry.owner_user_id == owner_user_id,
            TreeNoteDailyEntry.note_date == note_date,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @classmethod
    async def _ancestor_depth(cls, session: AsyncSession, owner_user_id: str, start_id: str) -> int:
        """Compute root-to-node depth (1-based) using read-only WITH RECURSIVE CTE.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            start_id: Starting node ID.

        Returns:
            1-based path length from root to ``start_id``.
        """
        return len(await cls._walk_ancestors(session, owner_user_id, start_id))

    @classmethod
    async def _walk_ancestors(cls, session: AsyncSession, owner_user_id: str, start_id: str) -> list[str]:
        """Return ordered ancestor IDs starting at ``start_id`` via read-only WITH RECURSIVE.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            start_id: Starting node ID.

        Returns:
            Ordered list of ancestor node IDs from ``start_id`` upward.
        """
        return await TreeNoteTreeHelpers.walk_ancestors(session, owner_user_id, start_id)

    @classmethod
    async def _check_cycle_and_depth(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        node_id: str,
        new_parent_id: str,
    ) -> None:
        """Reject ancestor cycles and moves exceeding MAX_TREE_DEPTH=64.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node_id: Moving node identifier.
            new_parent_id: Destination parent node identifier.

        Raises:
            RepoError: Move introduces a cycle or exceeds maximum depth.
        """
        ancestors = await TreeNoteTreeHelpers.walk_ancestors(session, owner_user_id, new_parent_id)
        if node_id in ancestors:
            raise cls.RepoError(409, 'cycle_detected', 'Moving node under its own descendant creates a cycle.', node_id)
        levels = await TreeNoteTreeHelpers.collect_subtree_levels(session, owner_user_id, node_id)
        if len(ancestors) + len(levels) > TreeNoteContracts.MAX_TREE_DEPTH:
            raise cls.RepoError(409, 'max_depth_exceeded', 'Move exceeds maximum tree depth (64).', node_id)

    @staticmethod
    async def _collect_subtree_levels(session: AsyncSession, owner_user_id: str, root_id: str) -> list[list[str]]:
        """Collect subtree node IDs ordered deepest-level-first using read-only CTE.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            root_id: Subtree root node ID.

        Returns:
            List of node-ID lists ordered deepest level first.
        """
        return await TreeNoteTreeHelpers.collect_subtree_levels(session, owner_user_id, root_id)

    @classmethod
    async def _apply_move_order(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        node: TreeNoteNode,
        request: TreeNoteContracts.MoveRequest,
    ) -> None:
        """Move ``node`` to ``request.new_parent_id`` and normalize sibling order safely.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            node: Target node ORM row to move.
            request: Validated move request carrying destination parent and optional position.
        """
        same_parent = node.parent_id == request.new_parent_id
        siblings = await cls._load_siblings(session, owner_user_id, request.new_parent_id)
        if not same_parent:
            if len(siblings) >= TreeNoteContracts.MAX_SIBLINGS_PER_PARENT:
                raise cls.RepoError(409, 'max_siblings_exceeded', 'Destination parent sibling limit exceeded.', node.id)
            node.parent_id = request.new_parent_id
            node.sort_order = max((s.sort_order for s in siblings), default=-1) + 1
            await session.flush()
            siblings.append(node)
        if request.target_position is not None or not same_parent:
            ordered = [s for s in siblings if s.id != node.id]
            dest = len(ordered) if request.target_position is None else min(request.target_position, len(ordered))
            ordered.insert(dest, node)
            await cls._two_phase_node_reorder(session, ordered)

    @staticmethod
    async def _two_phase_node_reorder(session: AsyncSession, ordered_rows: list[TreeNoteNode]) -> None:
        """Apply disjoint positive-offset two-phase sort_order rewrite on sibling nodes.

        Args:
            session: Active ``AsyncSession``.
            ordered_rows: Sibling ``TreeNoteNode`` rows in target order.
        """
        await TreeNoteTreeHelpers.two_phase_node_reorder(session, ordered_rows)

    @staticmethod
    def _insert_parsed_parts(session: AsyncSession, node_id: str, markdown: str, updated_at: int) -> None:
        """Insert parsed Markdown parts for a newly created node.

        Args:
            session: Active ``AsyncSession``.
            node_id: Owning node ID.
            markdown: Initial Markdown text.
            updated_at: Nanosecond timestamp applied to inserted parts.
        """
        TreeNoteTreeHelpers.insert_parsed_parts(session, node_id, markdown, updated_at)

    @classmethod
    async def _delete_node_dependents(cls, session: AsyncSession, node_ids: list[str]) -> None:
        """Explicitly delete external_refs, tags, parts, and daily_entries for SQLite/PG safety.

        Args:
            session: Active ``AsyncSession``.
            node_ids: Node identifiers whose child dependent rows are being deleted.
        """
        await TreeNoteTreeHelpers.delete_node_dependents(session, node_ids)

    @staticmethod
    def _depth_levels_bottom_up(parent_map: dict[str, str | None]) -> list[list[str]]:
        """Group node IDs by tree depth ordered deepest-first for RESTRICT-safe deletion.

        Args:
            parent_map: Mapping from ``node_id`` to optional ``parent_id``.

        Returns:
            List of node-ID buckets sorted deepest depth first.
        """
        return TreeNoteTreeHelpers.depth_levels_bottom_up(parent_map)

    @staticmethod
    def _chunks(items: list[str], size: int = 500) -> list[list[str]]:
        """Split a list of IDs into chunks safe for SQLite variable limits.

        Args:
            items: Full list of identifier strings.
            size: Maximum slice length per chunk.

        Returns:
            List of contiguous ID slices.
        """
        return TreeNoteTreeHelpers.chunks(items, size=size)
