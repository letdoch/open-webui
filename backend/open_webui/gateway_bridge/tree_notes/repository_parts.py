"""Serialized per-owner transactional mutations for Markdown parts and full documents."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.repository import TreeNoteRepository
from open_webui.internal.db import get_async_db


class TreeNotePartRepository:
    """Per-owner serialized transactional mutations for ``tree_note_part`` trees."""

    class _ReconcilePlan(BaseModel):
        """Internal payload describing a UUID-preserving Markdown reconciliation plan."""

        model_config = ConfigDict(strict=True, arbitrary_types_allowed=True, extra='forbid')

        existing_parts: list[TreeNotePart]
        reconciled: list[TreeNotePartsCodec.ReconciledPart]
        deleted_ids: list[str]
        now_ns: int

    @classmethod
    async def put_markdown(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNoteContracts.MarkdownPutRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Replace a node's Markdown content while preserving unchanged/matching part UUIDs.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Target node identifier.
            request: CAS-protected Markdown replacement payload.

        Returns:
            Updated ``ReadResponse``.
        """
        async with get_async_db() as session:
            await TreeNoteRepository.acquire_owner_lock(session, owner_user_id)
            node = await TreeNoteRepository.require_owned_node(session, owner_user_id, node_id)
            if node.archived_at is not None:
                raise TreeNoteRepository.RepoError(409, 'node_archived', 'Cannot edit an archived node.', node.id)
            existing_parts = await TreeNoteRepository.load_parts(session, node.id)
            TreeNoteRepository.verify_node_cas(node, existing_parts, request.expected_basis_sha256)
            parsed = TreeNotePartsCodec.parse_markdown(request.markdown)
            reconciled, deleted_ids = TreeNotePartsCodec.reconcile_parts(existing_parts, parsed)
            now_ns = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            plan = cls._ReconcilePlan(
                existing_parts=existing_parts,
                reconciled=reconciled,
                deleted_ids=deleted_ids,
                now_ns=now_ns,
            )
            await cls._apply_reconciled_parts(session, node.id, plan)
            node.updated_at = now_ns
            await session.commit()
            return await TreeNoteRepository.must_read(session, owner_user_id, node.id)

    @classmethod
    async def create_part(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNotePartContracts.PartCreateRequest,
    ) -> TreeNotePartContracts.PartMutationResponse:
        """Insert one addressable block part into a node under CAS protection.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.
            request: Validated part creation request.

        Returns:
            ``PartMutationResponse`` with refreshed node and created ``PartDTO``.
        """
        async with get_async_db() as session:
            await TreeNoteRepository.acquire_owner_lock(session, owner_user_id)
            node = await TreeNoteRepository.require_owned_node(session, owner_user_id, node_id)
            parent_part: TreeNotePart | None = None
            if request.parent_part_id is not None:
                parent_part = await cls._require_owned_part(session, node.id, request.parent_part_id)
            cls._guard_part_creation(node, request, parent_part)
            parts = await TreeNoteRepository.load_parts(session, node.id)
            cls._verify_container_cas(node, parts, parent_part, (request.expected_basis_sha256, request.expected_part_basis_sha256))
            new_part_id = await cls._insert_part_at_position(session, node, parts, request)
            await session.commit()
            read = await TreeNoteRepository.must_read(session, owner_user_id, node.id)
            created_dto = next(p for p in read.parts if p.id == new_part_id)
            return TreeNotePartContracts.PartMutationResponse(node=read, part=created_dto)

    @staticmethod
    def _guard_part_creation(
        node: TreeNoteNode,
        request: TreeNotePartContracts.PartCreateRequest,
        parent_part: TreeNotePart | None,
    ) -> None:
        """Enforce non-archived node state and valid parent/child part nesting.

        Args:
            node: Loaded owner ``TreeNoteNode``.
            request: Validated part creation request.
            parent_part: Optional parent ``TreeNotePart`` row.

        Raises:
            TreeNoteRepository.RepoError: Node is archived or parent-child nesting is invalid.
        """
        if node.archived_at is not None:
            raise TreeNoteRepository.RepoError(409, 'node_archived', 'Cannot edit parts of archived node.', node.id)
        try:
            TreeNotePartsCodec.validate_parent_child_kind(
                parent_part.part_kind if parent_part is not None else None,
                request.part_kind,
                parent_part.heading_level if parent_part is not None else None,
                request.heading_level,
            )
        except ValueError as exc:
            raise TreeNoteRepository.RepoError(422, 'invalid_part_nesting', str(exc), node.id) from exc

    @classmethod
    async def _insert_part_at_position(
        cls,
        session: AsyncSession,
        node: TreeNoteNode,
        parts: list[TreeNotePart],
        request: TreeNotePartContracts.PartCreateRequest,
    ) -> str:
        """Persist a new part row and reindex container siblings via two-phase ordering.

        Args:
            session: Active serialized ``AsyncSession``.
            node: Target owning ``TreeNoteNode``.
            parts: Existing loaded ``TreeNotePart`` list for ``node``.
            request: Validated part creation request.

        Returns:
            Newly created part UUID string.

        Raises:
            TreeNoteRepository.RepoError: Sibling count limit exceeded.
        """
        now_ns = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
        siblings = [p for p in parts if p.parent_part_id == request.parent_part_id]
        if len(siblings) >= TreeNoteContracts.MAX_SIBLINGS_PER_PARENT:
            raise TreeNoteRepository.RepoError(409, 'max_siblings_exceeded', 'Sibling limit exceeded.', node.id)
        temp_pos = max((p.position for p in siblings), default=-1) + 1
        new_part = cls._build_part_row(node.id, request, temp_pos, now_ns)
        session.add(new_part)
        await session.flush()
        insert_at = len(siblings) if request.position is None else min(request.position, len(siblings))
        siblings.insert(insert_at, new_part)
        await cls._two_phase_part_reorder(session, siblings)
        node.updated_at = now_ns
        return new_part.id

    @classmethod
    async def update_part(
        cls,
        owner_user_id: str,
        node_id: str,
        part_id: str,
        request: TreeNotePartContracts.PartUpdateRequest,
    ) -> TreeNotePartContracts.PartMutationResponse:
        """Update one part under strict part-level optimistic concurrency.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.
            part_id: Target part identifier within ``node_id``.
            request: CAS-protected part patch payload.

        Returns:
            ``PartMutationResponse`` with refreshed node and updated part.
        """
        async with get_async_db() as session:
            await TreeNoteRepository.acquire_owner_lock(session, owner_user_id)
            node = await TreeNoteRepository.require_owned_node(session, owner_user_id, node_id)
            part = await cls._require_owned_part(session, node.id, part_id)
            if node.archived_at is not None:
                raise TreeNoteRepository.RepoError(409, 'node_archived', 'Cannot edit parts of archived node.', node.id)
            parts = await TreeNoteRepository.load_parts(session, node.id)
            cls._validate_part_patch_invariants(part, parts, request)
            cls._verify_part_cas(node, part, parts, request.expected_part_basis_sha256)
            now_ns = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            cls._apply_part_patch(part, request, now_ns)
            node.updated_at = now_ns
            await session.commit()
            read = await TreeNoteRepository.must_read(session, owner_user_id, node.id)
            updated_dto = next(p for p in read.parts if p.id == part.id)
            return TreeNotePartContracts.PartMutationResponse(node=read, part=updated_dto)

    @classmethod
    async def reorder_parts(
        cls,
        owner_user_id: str,
        node_id: str,
        request: TreeNotePartContracts.PartReorderRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Reorder sibling parts within a node container under CAS protection.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.
            request: Reorder sequence, expected sibling list, and CAS digest.

        Returns:
            Refreshed ``ReadResponse``.
        """
        async with get_async_db() as session:
            await TreeNoteRepository.acquire_owner_lock(session, owner_user_id)
            node = await TreeNoteRepository.require_owned_node(session, owner_user_id, node_id)
            parent_part: TreeNotePart | None = None
            if request.parent_part_id is not None:
                parent_part = await cls._require_owned_part(session, node.id, request.parent_part_id)
            for pid in request.ordered_part_ids:
                await cls._require_owned_part(session, node.id, pid)
            if node.archived_at is not None:
                raise TreeNoteRepository.RepoError(
                    409,
                    'node_archived',
                    'Cannot reorder parts of an archived node.',
                    node.id,
                )

            parts = await TreeNoteRepository.load_parts(session, node.id)
            siblings = [p for p in parts if p.parent_part_id == request.parent_part_id]
            live_ids = [p.id for p in siblings]
            if live_ids != request.expected_sibling_part_ids:
                raise TreeNoteRepository.RepoError(
                    409,
                    'sibling_conflict',
                    'Sibling part list changed concurrently.',
                    node.id,
                )
            cas_pair = (request.expected_basis_sha256, request.expected_part_basis_sha256)
            cls._verify_container_cas(node, parts, parent_part, cas_pair)
            by_id = {p.id: p for p in siblings}
            await cls._two_phase_part_reorder(session, [by_id[pid] for pid in request.ordered_part_ids])
            node.updated_at = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            await session.commit()
            return await TreeNoteRepository.must_read(session, owner_user_id, node.id)

    @classmethod
    async def delete_part(
        cls,
        owner_user_id: str,
        node_id: str,
        part_id: str,
        request: TreeNotePartContracts.PartDeleteRequest,
    ) -> TreeNotePartContracts.PartDeleteResponse:
        """Delete a part and its nested child parts bottom-up under part CAS.

        Args:
            owner_user_id: Verified owner user ID.
            node_id: Owning node identifier.
            part_id: Target part identifier within ``node_id``.
            request: Expected ``part_basis_sha256`` digest.

        Returns:
            ``PartDeleteResponse`` with deleted part IDs and refreshed node.
        """
        async with get_async_db() as session:
            await TreeNoteRepository.acquire_owner_lock(session, owner_user_id)
            node = await TreeNoteRepository.require_owned_node(session, owner_user_id, node_id)
            part = await cls._require_owned_part(session, node.id, part_id)
            if node.archived_at is not None:
                raise TreeNoteRepository.RepoError(
                    409,
                    'node_archived',
                    'Cannot delete parts of an archived node.',
                    node.id,
                )
            parts = await TreeNoteRepository.load_parts(session, node.id)
            cls._verify_part_cas(node, part, parts, request.expected_part_basis_sha256)
            deleted_ids = cls._collect_part_subtree_bottom_up(parts, part.id)
            for pid in deleted_ids:
                await session.execute(
                    delete(TreeNotePart).where(TreeNotePart.node_id == node.id, TreeNotePart.id == pid)
                )
            node.updated_at = TreeNoteDigest.next_monotonic_ns(node.updated_at, node.created_at)
            await session.commit()
            read = await TreeNoteRepository.must_read(session, owner_user_id, node.id)
            return TreeNotePartContracts.PartDeleteResponse(deleted_part_ids=deleted_ids, node=read)

    @staticmethod
    def _build_part_row(
        node_id: str,
        request: TreeNotePartContracts.PartCreateRequest,
        temp_pos: int,
        now_ns: int,
    ) -> TreeNotePart:
        """Construct a ``TreeNotePart`` ORM instance from validated request data.

        Args:
            node_id: Owning node UUID.
            request: Validated creation request fields.
            temp_pos: Initial sibling position before dense normalization.
            now_ns: Monotonic nanosecond update timestamp.

        Returns:
            Unsaved ``TreeNotePart`` SQLAlchemy ORM row.
        """
        return TreeNotePart(
            id=TreeNoteDigest.new_uuid7(),
            node_id=node_id,
            parent_part_id=request.parent_part_id,
            position=temp_pos,
            part_kind=request.part_kind,
            heading_level=request.heading_level,
            title=request.title,
            content_markdown=request.content_markdown,
            list_style=request.list_style,
            language=request.language,
            checked=request.checked,
            updated_at=now_ns,
        )

    @staticmethod
    async def _require_owned_part(session: AsyncSession, node_id: str, part_id: str) -> TreeNotePart:
        """Return a ``TreeNotePart`` strictly scoped to ``node_id`` or raise 404.

        Args:
            session: Active ``AsyncSession``.
            node_id: Owning node ID.
            part_id: Target part ID.

        Returns:
            Matching ``TreeNotePart`` row.

        Raises:
            TreeNoteRepository.RepoError: Part does not exist on ``node_id``.
        """
        part = (
            await session.execute(
                select(TreeNotePart).where(TreeNotePart.id == part_id, TreeNotePart.node_id == node_id)
            )
        ).scalar_one_or_none()
        if part is None:
            raise TreeNoteRepository.RepoError(404, 'not_found', 'Tree note part not found.', node_id)
        return part

    @staticmethod
    def _verify_part_cas(
        node: TreeNoteNode,
        part: TreeNotePart,
        parts: list[TreeNotePart],
        expected_part_basis_sha256: str,
    ) -> None:
        """Verify part-level CAS digest and raise structured 409 conflict on mismatch.

        Args:
            node: Loaded owner ``TreeNoteNode``.
            part: Target ``TreeNotePart`` row.
            parts: All ``TreeNotePart`` rows owned by ``node``.
            expected_part_basis_sha256: Caller expected part digest.

        Raises:
            TreeNoteRepository.RepoError: HTTP 409 conflict when digest differs.
        """
        current_basis, part_digests = TreeNoteDigest.compute_node_digest(node, parts)
        current_part_basis = part_digests[part.id]
        if current_part_basis != expected_part_basis_sha256:
            conflict = TreeNoteContracts.BasisConflictResponse(
                node_id=node.id,
                expected_basis_sha256=None,
                current_basis_sha256=current_basis,
                part_id=part.id,
                expected_part_basis_sha256=expected_part_basis_sha256,
                current_part_basis_sha256=current_part_basis,
                current_updated_at=node.updated_at,
            )
            raise TreeNoteRepository.RepoError.from_conflict(conflict, 'Part changed concurrently.')

    @classmethod
    def _verify_container_cas(
        cls,
        node: TreeNoteNode,
        parts: list[TreeNotePart],
        parent_part: TreeNotePart | None,
        cas_pair: tuple[str | None, str | None],
    ) -> None:
        """Verify whole-node or parent-part CAS for insertion/reorder inside a container.

        Args:
            node: Loaded owner ``TreeNoteNode``.
            parts: All ``TreeNotePart`` rows owned by ``node``.
            parent_part: Target parent container part, or ``None`` for document root.
            cas_pair: Tuple of ``(expected_basis_sha256, expected_part_basis_sha256)``.

        Raises:
            TreeNoteRepository.RepoError: Missing CAS digest or CAS mismatch.
        """
        expected_basis, expected_part_basis = cas_pair
        if parent_part is not None and expected_part_basis is not None:
            cls._verify_part_cas(node, parent_part, parts, expected_part_basis)
            return
        if expected_basis is not None:
            TreeNoteRepository.verify_node_cas(node, parts, expected_basis)
            return
        raise TreeNoteRepository.RepoError(422, 'cas_required', 'Expected CAS digest is required.', node.id)

    @staticmethod
    async def _two_phase_part_reorder(session: AsyncSession, ordered_parts: list[TreeNotePart]) -> None:
        """Apply disjoint positive-offset two-phase position rewrite on sibling parts.

        Args:
            session: Active ``AsyncSession``.
            ordered_parts: Sibling ``TreeNotePart`` rows in destination order.
        """
        if not ordered_parts:
            return
        offset = max(part.position for part in ordered_parts) + 1
        for idx, part in enumerate(ordered_parts):
            part.position = offset + idx
        await session.flush()
        for idx, part in enumerate(ordered_parts):
            part.position = idx
        await session.flush()

    @staticmethod
    async def _apply_reconciled_parts(
        session: AsyncSession,
        node_id: str,
        plan: TreeNotePartRepository._ReconcilePlan,
    ) -> None:
        """Apply UUID-preserving reconciliation without colliding on unique position indexes.

        Args:
            session: Active ``AsyncSession``.
            node_id: Target node identifier.
            plan: Reconciliation plan containing surviving, added, and removed parts.
        """
        for pid in plan.deleted_ids:
            await session.execute(delete(TreeNotePart).where(TreeNotePart.node_id == node_id, TreeNotePart.id == pid))
        await session.flush()

        surviving_map = {part.id: part for part in plan.existing_parts if part.id not in set(plan.deleted_ids)}
        temp_offset = max((p.position for p in surviving_map.values()), default=-1) + 1
        for idx, part in enumerate(surviving_map.values()):
            part.position = temp_offset + idx
        await session.flush()

        new_seq = temp_offset + len(surviving_map)
        for item in plan.reconciled:
            if not item.is_new:
                continue
            new_seq += 1
            session.add(
                TreeNotePart(
                    id=item.part_id,
                    node_id=node_id,
                    parent_part_id=item.parent_part_id,
                    position=new_seq,
                    part_kind=item.part_kind,
                    heading_level=item.heading_level,
                    title=item.title,
                    content_markdown=item.content_markdown,
                    list_style=item.list_style,
                    language=item.language,
                    checked=item.checked,
                    updated_at=plan.now_ns,
                )
            )
        await session.flush()

        for item in plan.reconciled:
            if item.is_new:
                continue
            row = surviving_map[item.part_id]
            row.parent_part_id = item.parent_part_id
            row.part_kind = item.part_kind
            row.heading_level = item.heading_level
            row.title = item.title
            row.content_markdown = item.content_markdown
            row.list_style = item.list_style
            row.language = item.language
            row.checked = item.checked
            row.updated_at = plan.now_ns
        await session.flush()

        all_rows = list(
            (await session.execute(select(TreeNotePart).where(TreeNotePart.node_id == node_id))).scalars().all()
        )
        row_by_id = {row.id: row for row in all_rows}
        for item in plan.reconciled:
            row_by_id[item.part_id].position = item.position
        await session.flush()

    @classmethod
    def _validate_part_patch_invariants(
        cls,
        part: TreeNotePart,
        parts: list[TreeNotePart],
        request: TreeNotePartContracts.PartUpdateRequest,
    ) -> None:
        """Ensure partial updates obey per-kind DB constraints and section heading hierarchy.

        Args:
            part: Existing target ``TreeNotePart``.
            parts: All parts owned by ``part.node_id``.
            request: Validated partial update payload.

        Raises:
            TreeNoteRepository.RepoError: Requested patch violates part kind constraints.
        """
        kind = part.part_kind
        if kind == 'section':
            cls._validate_section_patch(part, parts, request)
        elif request.heading_level is not None or request.title is not None:
            raise TreeNoteRepository.RepoError(
                422,
                'invalid_part_field',
                'heading_level and title apply only to sections.',
                part.node_id,
            )
        if kind != 'list' and request.list_style is not None:
            raise TreeNoteRepository.RepoError(
                422,
                'invalid_part_field',
                'list_style applies only to lists.',
                part.node_id,
            )
        if kind != 'fence' and (request.language is not None or request.clear_language):
            raise TreeNoteRepository.RepoError(
                422,
                'invalid_part_field',
                'language applies only to fence blocks.',
                part.node_id,
            )
        if kind != 'item' and (request.checked is not None or request.clear_checked):
            raise TreeNoteRepository.RepoError(
                422,
                'invalid_part_field',
                'checked applies only to list items.',
                part.node_id,
            )
        if request.content_markdown is not None:
            if kind in ('section', 'list', 'rule') and request.content_markdown != '':
                raise TreeNoteRepository.RepoError(
                    422,
                    'invalid_part_field',
                    f'{kind} requires empty content_markdown.',
                    part.node_id,
                )
            if kind in ('paragraph', 'table') and not request.content_markdown.strip():
                raise TreeNoteRepository.RepoError(
                    422,
                    'invalid_part_field',
                    f'{kind} requires non-empty content_markdown.',
                    part.node_id,
                )

    @staticmethod
    def _validate_section_patch(
        part: TreeNotePart,
        parts: list[TreeNotePart],
        request: TreeNotePartContracts.PartUpdateRequest,
    ) -> None:
        """Check section heading_level ordering against parent and child sections.

        Args:
            part: Existing section ``TreeNotePart``.
            parts: All parts owned by ``part.node_id``.
            request: Validated update request.

        Raises:
            TreeNoteRepository.RepoError: Heading level conflicts with parent or child sections.
        """
        if request.heading_level is None:
            return
        by_id = {p.id: p for p in parts}
        if part.parent_part_id is not None and part.parent_part_id in by_id:
            parent = by_id[part.parent_part_id]
            if parent.part_kind == 'section' and parent.heading_level is not None:
                if request.heading_level <= parent.heading_level:
                    raise TreeNoteRepository.RepoError(
                        422,
                        'invalid_heading_level',
                        'heading_level must exceed parent.',
                        part.node_id,
                    )
        for child in parts:
            if child.parent_part_id == part.id and child.part_kind == 'section' and child.heading_level is not None:
                if request.heading_level >= child.heading_level:
                    raise TreeNoteRepository.RepoError(
                        422,
                        'invalid_heading_level',
                        'heading_level must precede children.',
                        part.node_id,
                    )

    @staticmethod
    def _apply_part_patch(part: TreeNotePart, request: TreeNotePartContracts.PartUpdateRequest, now_ns: int) -> None:
        """Mutate validated fields on ``part``.

        Args:
            part: Persistent ``TreeNotePart`` row to mutate.
            request: Validated partial update payload.
            now_ns: Monotonic nanosecond update timestamp.
        """
        if request.heading_level is not None:
            part.heading_level = request.heading_level
        if request.title is not None:
            part.title = request.title
        if request.content_markdown is not None:
            part.content_markdown = request.content_markdown
        if request.list_style is not None:
            part.list_style = request.list_style
        if request.clear_language:
            part.language = None
        elif request.language is not None:
            part.language = request.language
        if request.clear_checked:
            part.checked = None
        elif request.checked is not None:
            part.checked = request.checked
        part.updated_at = now_ns

    @staticmethod
    def _collect_part_subtree_bottom_up(parts: list[TreeNotePart], root_part_id: str) -> list[str]:
        """Collect part IDs inside ``root_part_id`` subtree ordered deepest-first.

        Args:
            parts: All ``TreeNotePart`` rows belonging to the node.
            root_part_id: Subtree root part UUID.

        Returns:
            List of descendant-and-root part IDs ordered deepest-first.
        """
        children_map: dict[str | None, list[str]] = {}
        for part in parts:
            children_map.setdefault(part.parent_part_id, []).append(part.id)
        levels: dict[int, list[str]] = {}
        queue: list[tuple[str, int]] = [(root_part_id, 0)]
        seen: set[str] = set()
        while queue:
            pid, depth = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            levels.setdefault(depth, []).append(pid)
            for child_id in (children_map[pid] if pid in children_map else []):
                queue.append((child_id, depth + 1))
        return [pid for depth in sorted(levels.keys(), reverse=True) for pid in levels[depth]]
