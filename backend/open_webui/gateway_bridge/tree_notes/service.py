"""Application service enforcing permissions, file access, timezones, and transactions."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status

from open_webui.constants import ERROR_MESSAGES
from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.gateway_bridge.media_session_auth import GatewayMediaSessionAuth
from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.promote_client import TreeNotePromoteClient
from open_webui.gateway_bridge.tree_notes.repository import TreeNoteRepository
from open_webui.gateway_bridge.tree_notes.repository_parts import TreeNotePartRepository
from open_webui.gateway_bridge.tree_notes.repository_queries import TreeNoteQueries
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.groups import Groups
from open_webui.models.users import UserModel
from open_webui.utils.access_control import has_permission
from open_webui.utils.access_control.files import has_access_to_file


class TreeNoteService:
    """Authorized orchestration layer for tree note HTTP routes and assistant tools."""

    @staticmethod
    def resolve_settings(request: Request) -> GatewayBridgeSettings:
        """Resolve gateway bridge settings cached on ``app.state``.

        Args:
            request: Active FastAPI HTTP request.

        Returns:
            Validated ``GatewayBridgeSettings``.
        """
        return GatewayMediaSessionAuth.resolve_settings(request)

    @classmethod
    def get_status(cls, request: Request) -> TreeNoteContracts.StatusResponse:
        """Return runtime availability of tree notes and promotion for the frontend.

        Args:
            request: Active FastAPI HTTP request.

        Returns:
            ``StatusResponse`` indicating enabled status.
        """
        settings = cls.resolve_settings(request)
        enabled = bool(settings.enabled and settings.tree_notes_enabled)
        promote_enabled = bool(enabled and settings.normalized_tree_notes_promote_url())
        return TreeNoteContracts.StatusResponse(enabled=enabled, promote_enabled=promote_enabled)

    @classmethod
    async def ensure_access(cls, request: Request, user: UserModel) -> GatewayBridgeSettings:
        """Enforce gateway bridge enablement, ``notes.enable``, and ``features.notes`` permission.

        Args:
            request: Active FastAPI HTTP request.
            user: Verified authenticated user.

        Returns:
            Active ``GatewayBridgeSettings``.

        Raises:
            HTTPException: Feature disabled (404/403) or permission denied (401).
        """
        settings = cls.resolve_settings(request)
        if not settings.enabled or not settings.tree_notes_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Tree notes feature is disabled.')
        if not await Config.get('notes.enable'):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.FEATURE_DISABLED('Notes'))
        if user.role != 'admin':
            async with get_async_db() as session:
                perms = await Config.get('user.permissions')
                allowed = await has_permission(user.id, 'features.notes', perms, db=session)
            if not allowed:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)
        return settings

    @classmethod
    async def check_added_file_links(
        cls,
        user: UserModel,
        existing_node: TreeNotePartContracts.ReadResponse | None,
        new_markdown: str,
    ) -> None:
        """Verify caller read access to any newly introduced ``/api/v1/files/{id}/content`` links.

        Args:
            user: Verified caller user model.
            existing_node: Prior node snapshot when editing, or ``None`` on creation.
            new_markdown: Incoming Markdown text containing potential file references.

        Raises:
            HTTPException: Caller lacks read access to one or more newly linked files (403).
        """
        incoming_ids = TreeNotePartsCodec.extract_file_ids(new_markdown)
        existing_ids = (
            TreeNotePartsCodec.extract_file_ids(existing_node.rendered_markdown) if existing_node is not None else set()
        )
        added_ids = incoming_ids - existing_ids
        if not added_ids:
            return
        async with get_async_db() as session:
            groups = await Groups.get_groups_by_member_id(user.id, db=session, include_inherited=True)
            group_ids = {group.id for group in groups}
            for file_id in added_ids:
                file_row = await Files.get_file_by_id(file_id, db=session)
                if file_row is None:
                    raise HTTPException(status_code=403, detail='Cannot attach files you cannot access.')
                if file_row.user_id == user.id:
                    continue
                can_read = await has_access_to_file(file_id, 'read', user, db=session, user_group_ids=group_ids)
                if not can_read:
                    raise HTTPException(status_code=403, detail='Cannot attach files you cannot access.')

    @classmethod
    async def open_today(
        cls,
        user: UserModel,
        requested_tz: str | None,
    ) -> TreeNotePartContracts.DailyResponse:
        """Resolve today's calendar date via ``tz -> User.timezone -> 'UTC'`` and get-or-create daily note.

        Args:
            user: Verified caller user model.
            requested_tz: Optional caller-specified IANA timezone string.

        Returns:
            ``DailyResponse`` for the resolved calendar date.

        Raises:
            HTTPException: Caller-provided ``tz`` is not a valid IANA zone (422).
        """
        zone_name = requested_tz
        if zone_name is not None:
            try:
                zone = ZoneInfo(zone_name)
            except ZoneInfoNotFoundError as exc:
                raise HTTPException(status_code=422, detail=f'Invalid IANA timezone: {zone_name}') from exc
        else:
            stored_tz = await TreeNoteRepository.get_user_timezone(user.id)
            try:
                zone = ZoneInfo(stored_tz)
            except ZoneInfoNotFoundError:
                zone = ZoneInfo('UTC')
        today_iso = dt.datetime.now(zone).date().isoformat()
        return await TreeNoteRepository.get_or_create_daily(user.id, today_iso)

    @classmethod
    async def create_node(
        cls,
        user: UserModel,
        body: TreeNoteContracts.CreateRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Create a node after checking parent existence and read access to newly attached file links.

        Args:
            user: Verified caller user model.
            body: Validated node creation request.

        Returns:
            Created ``ReadResponse``.

        Raises:
            HTTPException: Parent missing/foreign (404) or unauthorized file link (403).
        """
        if body.parent_id is not None:
            parent = await TreeNoteQueries.read_node(user.id, body.parent_id, include_parts=False)
            if parent is None:
                raise HTTPException(status_code=404, detail='Parent tree note node not found.')
        await cls.check_added_file_links(user, None, body.markdown)
        return await TreeNoteRepository.create_node(user.id, body)

    @classmethod
    async def put_markdown(
        cls,
        user: UserModel,
        node_id: str,
        body: TreeNoteContracts.MarkdownPutRequest,
    ) -> TreeNotePartContracts.ReadResponse:
        """Replace Markdown after checking node ownership and newly added file links.

        Args:
            user: Verified caller user model.
            node_id: Target node identifier.
            body: Validated Markdown replacement request.

        Returns:
            Updated ``ReadResponse``.

        Raises:
            HTTPException: Target node missing/foreign (404) or unauthorized file link (403).
        """
        existing = await TreeNoteQueries.read_node(user.id, node_id, include_parts=False)
        if existing is None:
            raise HTTPException(status_code=404, detail='Tree note node not found.')
        await cls.check_added_file_links(user, existing, body.markdown)
        return await TreeNotePartRepository.put_markdown(user.id, node_id, body)

    @classmethod
    async def create_part(
        cls,
        user: UserModel,
        node_id: str,
        body: TreeNotePartContracts.PartCreateRequest,
    ) -> TreeNotePartContracts.PartMutationResponse:
        """Create a single part after verifying ownership and newly attached file links.

        Args:
            user: Verified caller user model.
            node_id: Target node identifier.
            body: Validated part creation request.

        Returns:
            ``PartMutationResponse``.

        Raises:
            HTTPException: Missing/foreign node or parent part (404), or unauthorized file link (403).
        """
        existing = await TreeNoteQueries.read_node(user.id, node_id, include_parts=True)
        if existing is None:
            raise HTTPException(status_code=404, detail='Tree note node not found.')
        if body.parent_part_id is not None and not any(p.id == body.parent_part_id for p in existing.parts):
            raise HTTPException(status_code=404, detail='Tree note parent part not found.')
        await cls.check_added_file_links(user, existing, body.content_markdown)
        return await TreeNotePartRepository.create_part(user.id, node_id, body)

    @classmethod
    async def update_part(
        cls,
        user: UserModel,
        node_id: str,
        part_id: str,
        body: TreeNotePartContracts.PartUpdateRequest,
    ) -> TreeNotePartContracts.PartMutationResponse:
        """Update a single part after verifying ownership and newly attached file links.

        Args:
            user: Verified caller user model.
            node_id: Target node identifier.
            part_id: Target part identifier.
            body: Validated part update payload.

        Returns:
            ``PartMutationResponse``.

        Raises:
            HTTPException: Missing/foreign node or part (404), or unauthorized file link (403).
        """
        existing = await TreeNoteQueries.read_node(user.id, node_id, include_parts=True)
        if existing is None:
            raise HTTPException(status_code=404, detail='Tree note node not found.')
        if not any(p.id == part_id for p in existing.parts):
            raise HTTPException(status_code=404, detail='Tree note part not found.')
        if body.content_markdown is not None:
            await cls.check_added_file_links(user, existing, body.content_markdown)
        return await TreeNotePartRepository.update_part(user.id, node_id, part_id, body)

    @classmethod
    async def promote_node(
        cls,
        request: Request,
        user: UserModel,
        node_id: str,
        body: TreeNotePartContracts.PromoteRequest,
    ) -> TreeNotePartContracts.PromoteResponse:
        """Promote a node to an external task or idea via caller ``system_oauth``.

        Args:
            request: Active FastAPI HTTP request.
            user: Verified caller user model.
            node_id: Target node identifier.
            body: Confirmed promotion request.

        Returns:
            Verified ``PromoteResponse``.

        Raises:
            HTTPException: Promotion URL unconfigured (503).
        """
        settings = cls.resolve_settings(request)
        if not settings.normalized_tree_notes_promote_url():
            raise HTTPException(status_code=503, detail='Tree notes promotion endpoint is not configured.')
        snapshot = await TreeNoteRepository.capture_promote_snapshot(user.id, node_id, body.expected_basis_sha256)
        external_id, target_url, status_text = await TreeNotePromoteClient.promote(request, user, snapshot, body)
        record_input = TreeNoteRepository.PromoteRecordInput(
            node_id=node_id,
            target_kind=body.target_kind,
            external_id=external_id,
            expected_basis_sha256=body.expected_basis_sha256,
        )
        ref = await TreeNoteRepository.record_promoted_ref(user.id, record_input)
        return TreeNotePartContracts.PromoteResponse(ref=ref, target_url=target_url, status=status_text)
