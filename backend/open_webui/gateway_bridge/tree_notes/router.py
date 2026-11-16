"""FastAPI router exposing Trilium-style tree notes and daily calendar APIs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import ValidationError

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.repository import TreeNoteRepository
from open_webui.gateway_bridge.tree_notes.repository_parts import TreeNotePartRepository
from open_webui.gateway_bridge.tree_notes.repository_queries import TreeNoteQueries
from open_webui.gateway_bridge.tree_notes.service import TreeNoteService
from open_webui.models.users import UserModel
from open_webui.utils.auth import get_verified_user


class TreeNoteRouter:
    """Builds the owner-scoped FastAPI router for ``/api/v1/gateway-bridge/tree-notes``."""

    @staticmethod
    def _error_response(exc: TreeNoteRepository.RepoError) -> Response:
        """Convert a repository error into an HTTP JSON response.

        Args:
            exc: Raised domain repository exception with HTTP status and optional CAS conflict payload.

        Returns:
            Serialized JSON ``Response`` carrying the conflict or domain error model.
        """
        if exc.conflict is not None:
            return Response(
                status_code=exc.status_code,
                content=exc.conflict.model_dump_json(),
                media_type='application/json',
            )
        payload = TreeNoteContracts.DomainErrorResponse(
            error=exc.error_code,
            detail=exc.detail,
            node_id=exc.node_id,
        )
        return Response(
            status_code=exc.status_code,
            content=payload.model_dump_json(),
            media_type='application/json',
        )

    @classmethod
    def build(cls) -> APIRouter:
        """Construct the FastAPI ``APIRouter`` with every tree-notes endpoint.

        Returns:
            Configured ``APIRouter`` ready to mount at ``/api/v1/gateway-bridge/tree-notes``.
        """
        router = APIRouter()
        cls._register_read_routes(router)
        cls._register_daily_routes(router)
        cls._register_node_edit_routes(router)
        cls._register_node_move_routes(router)
        cls._register_node_lifecycle_routes(router)
        cls._register_part_edit_routes(router)
        cls._register_part_collection_routes(router)
        cls._register_tag_and_ref_routes(router)
        cls._register_promote_and_import_routes(router)
        return router

    @classmethod
    def _register_read_routes(cls, router: APIRouter) -> None:
        """Register status, tree, calendar, search, and node read endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.get('/status', response_model=TreeNoteContracts.StatusResponse)
        async def get_status(
            request: Request,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.StatusResponse:
            """Return whether tree notes and promotion are enabled."""
            _ = user
            return TreeNoteService.get_status(request)

        @router.get('/tree', response_model=TreeNoteContracts.TreeResponse)
        async def get_tree(
            request: Request,
            include_archived: bool = False,
            include_daily: bool = False,
            root_id: str | None = None,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.TreeResponse:
            """Return the owner-scoped hierarchical tree."""
            await TreeNoteService.ensure_access(request, user)
            clean_root = cls._validate_optional_id(root_id, 'root_id')
            params = TreeNoteQueries.TreeQueryParams(
                include_archived=include_archived,
                include_daily=include_daily,
                root_id=clean_root,
            )
            tree = await TreeNoteQueries.get_tree(user.id, params)
            if tree is None:
                raise HTTPException(status_code=404, detail='Tree note root not found.')
            return tree

        @router.get('/calendar', response_model=TreeNoteContracts.CalendarResponse)
        async def get_calendar(
            request: Request,
            year: int | None = Query(default=None, ge=1, le=9999),
            month: int | None = Query(default=None, ge=1, le=12),
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.CalendarResponse:
            """Return the projected Year / Month / Day calendar hierarchy."""
            await TreeNoteService.ensure_access(request, user)
            from_date = request.query_params.get('from_date')
            to_date = request.query_params.get('to_date')
            params = TreeNoteQueries.CalendarQueryParams(
                year=year,
                month=month,
                from_date=cls._validate_optional_date(from_date),
                to_date=cls._validate_optional_date(to_date),
            )
            return await TreeNoteQueries.get_calendar(user.id, params)

        @router.get('/search', response_model=TreeNoteContracts.SearchResponse)
        async def search_nodes(
            request: Request,
            query: str = '',
            tag: str | None = None,
            node_kind: Literal['folder', 'note', 'daily_note', 'task_draft'] | None = None,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.SearchResponse:
            """Search owner-scoped notes across titles, Markdown blocks, and tags."""
            await TreeNoteService.ensure_access(request, user)
            include_archived = request.query_params.get('include_archived', 'false').lower() == 'true'
            limit_raw = request.query_params.get('limit', '20')
            offset_raw = request.query_params.get('offset', '0')
            limit, offset = cls._parse_pagination(limit_raw, offset_raw)
            try:
                params = TreeNoteQueries.SearchQueryParams(
                    query=query,
                    tag=tag,
                    node_kind=node_kind,
                    include_archived=include_archived,
                    limit=limit,
                    offset=offset,
                )
                return await TreeNoteQueries.search(user.id, params)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @router.get('/nodes/{node_id}', response_model=TreeNotePartContracts.ReadResponse)
        async def read_node(
            request: Request,
            node_id: str,
            include_parts: bool = True,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse:
            """Read a single owned node with digests, rendered Markdown, parts, and refs."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            node = await TreeNoteQueries.read_node(user.id, clean_id, include_parts=include_parts)
            if node is None:
                raise HTTPException(status_code=404, detail='Tree note node not found.')
            return node

    @classmethod
    def _register_daily_routes(cls, router: APIRouter) -> None:
        """Register atomic daily calendar get-or-create endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/daily/today', response_model=TreeNotePartContracts.DailyResponse)
        async def open_today(
            request: Request,
            body: TreeNoteContracts.TodayRequest | None = Body(default=None),
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.DailyResponse | Response:
            """Get or create today's daily calendar entry resolved via timezone fallback."""
            await TreeNoteService.ensure_access(request, user)
            tz_value = body.tz if body is not None else None
            try:
                return await TreeNoteService.open_today(user, tz_value)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.post('/daily/{note_date}', response_model=TreeNotePartContracts.DailyResponse)
        async def open_daily_date(
            request: Request,
            note_date: str,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.DailyResponse | Response:
            """Atomically get or create the daily calendar root for ``note_date``."""
            await TreeNoteService.ensure_access(request, user)
            try:
                clean_date = TreeNoteContracts.clean_note_date(note_date)
                return await TreeNoteRepository.get_or_create_daily(user.id, clean_date)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_node_edit_routes(cls, router: APIRouter) -> None:
        """Register node creation, metadata patch, and Markdown replacement.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/nodes', response_model=TreeNotePartContracts.ReadResponse, status_code=status.HTTP_201_CREATED)
        async def create_node(
            request: Request,
            body: TreeNoteContracts.CreateRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Create a new folder, note, or task_draft node."""
            await TreeNoteService.ensure_access(request, user)
            try:
                return await TreeNoteService.create_node(user, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.patch('/nodes/{node_id}', response_model=TreeNotePartContracts.ReadResponse)
        async def update_node(
            request: Request,
            node_id: str,
            body: TreeNoteContracts.UpdateRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Update node title or non-daily kind with optimistic concurrency."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteRepository.update_node(user.id, clean_id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.put('/nodes/{node_id}/markdown', response_model=TreeNotePartContracts.ReadResponse)
        async def put_markdown(
            request: Request,
            node_id: str,
            body: TreeNoteContracts.MarkdownPutRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Replace node Markdown content and reconcile parts preserving matching UUIDs."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteService.put_markdown(user, clean_id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_node_move_routes(cls, router: APIRouter) -> None:
        """Register node move and sibling reorder endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/nodes/{node_id}/move', response_model=TreeNotePartContracts.ReadResponse)
        async def move_node(
            request: Request,
            node_id: str,
            body: TreeNoteContracts.MoveRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Move a node under another parent or reorder within its sibling group."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteRepository.move_node(user.id, clean_id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.post('/nodes/reorder', response_model=TreeNoteContracts.TreeResponse)
        async def reorder_nodes(
            request: Request,
            body: TreeNoteContracts.ReorderRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.TreeResponse | Response:
            """Reorder sibling nodes under a parent container."""
            await TreeNoteService.ensure_access(request, user)
            try:
                return await TreeNoteRepository.reorder_nodes(user.id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_node_lifecycle_routes(cls, router: APIRouter) -> None:
        """Register node archive and delete endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/nodes/{node_id}/archive', response_model=TreeNotePartContracts.ReadResponse)
        async def archive_node(
            request: Request,
            node_id: str,
            body: TreeNoteContracts.ArchiveRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Soft-archive or unarchive an owned node."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteRepository.archive_node(user.id, clean_id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.delete('/nodes/{node_id}', response_model=TreeNoteContracts.DeleteResponse)
        async def delete_node(
            request: Request,
            node_id: str,
            body: TreeNoteContracts.DeleteRequest | None = Body(default=None),
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNoteContracts.DeleteResponse | Response:
            """Delete an empty node or explicit recursive subtree."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            payload = cls._resolve_delete_node_request(request, body)
            try:
                return await TreeNoteRepository.delete_node(user.id, clean_id, payload)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_part_edit_routes(cls, router: APIRouter) -> None:
        """Register single-part create and patch endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post(
            '/nodes/{node_id}/parts',
            response_model=TreeNotePartContracts.PartMutationResponse,
            status_code=status.HTTP_201_CREATED,
        )
        async def create_part(
            request: Request,
            node_id: str,
            body: TreeNotePartContracts.PartCreateRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.PartMutationResponse | Response:
            """Create a new addressable part within a node."""
            await TreeNoteService.ensure_access(request, user)
            clean_id = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteService.create_part(user, clean_id, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.patch('/nodes/{node_id}/parts/{part_id}', response_model=TreeNotePartContracts.PartMutationResponse)
        async def update_part(
            request: Request,
            node_id: str,
            part_id: str,
            body: TreeNotePartContracts.PartUpdateRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.PartMutationResponse | Response:
            """Update an existing part by canonical UUID under part CAS."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            clean_part = cls._validate_id(part_id, 'part_id')
            try:
                return await TreeNoteService.update_part(user, clean_node, clean_part, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_part_collection_routes(cls, router: APIRouter) -> None:
        """Register part reorder and delete endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/nodes/{node_id}/parts/reorder', response_model=TreeNotePartContracts.ReadResponse)
        async def reorder_parts(
            request: Request,
            node_id: str,
            body: TreeNotePartContracts.PartReorderRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ReadResponse | Response:
            """Reorder sibling parts within a container."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNotePartRepository.reorder_parts(user.id, clean_node, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.delete('/nodes/{node_id}/parts/{part_id}', response_model=TreeNotePartContracts.PartDeleteResponse)
        async def delete_part(
            request: Request,
            node_id: str,
            part_id: str,
            body: TreeNotePartContracts.PartDeleteRequest | None = Body(default=None),
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.PartDeleteResponse | Response:
            """Delete a part subtree by canonical UUID under part CAS."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            clean_part = cls._validate_id(part_id, 'part_id')
            payload = cls._resolve_delete_part_request(request, body)
            try:
                return await TreeNotePartRepository.delete_part(user.id, clean_node, clean_part, payload)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_tag_and_ref_routes(cls, router: APIRouter) -> None:
        """Register tag replacement and external ref deletion endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.put('/nodes/{node_id}/tags', response_model=TreeNotePartContracts.TagsResponse)
        async def put_tags(
            request: Request,
            node_id: str,
            body: TreeNotePartContracts.TagsPutRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.TagsResponse | Response:
            """Replace tag slugs attached to an owned node."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteRepository.put_tags(user.id, clean_node, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.delete(
            '/nodes/{node_id}/external-refs/{ref_id}',
            response_model=TreeNotePartContracts.DeleteRefResponse,
        )
        async def delete_external_ref(
            request: Request,
            node_id: str,
            ref_id: str,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.DeleteRefResponse | Response:
            """Delete a user-promotable external reference link."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            clean_ref = cls._validate_id(ref_id, 'ref_id')
            try:
                return await TreeNoteRepository.delete_external_ref(user.id, clean_node, clean_ref)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @classmethod
    def _register_promote_and_import_routes(cls, router: APIRouter) -> None:
        """Register node promotion and legacy note import endpoints.

        Args:
            router: Target FastAPI router instance.
        """

        @router.post('/nodes/{node_id}/promote', response_model=TreeNotePartContracts.PromoteResponse)
        async def promote_node(
            request: Request,
            node_id: str,
            body: TreeNotePartContracts.PromoteRequest,
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.PromoteResponse | Response:
            """Promote a node to an external task or idea using caller OAuth bearer."""
            await TreeNoteService.ensure_access(request, user)
            clean_node = cls._validate_id(node_id, 'node_id')
            try:
                return await TreeNoteService.promote_node(request, user, clean_node, body)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

        @router.post('/import-legacy', response_model=TreeNotePartContracts.ImportLegacyResponse)
        async def import_legacy(
            request: Request,
            body: TreeNotePartContracts.ImportLegacyRequest | None = Body(default=None),
            user: UserModel = Depends(get_verified_user),
        ) -> TreeNotePartContracts.ImportLegacyResponse | Response:
            """Additively import the authenticated user's own legacy notes."""
            await TreeNoteService.ensure_access(request, user)
            payload = body if body is not None else TreeNotePartContracts.ImportLegacyRequest()
            try:
                return await TreeNoteRepository.import_legacy(user.id, payload)
            except TreeNoteRepository.RepoError as exc:
                return cls._error_response(exc)

    @staticmethod
    def _validate_id(value: str, field_name: str) -> str:
        """Validate a path/query identifier or raise HTTP 422.

        Args:
            value: Raw identifier string from URL path or query parameter.
            field_name: Logical parameter name for error reporting.

        Returns:
            Canonical validated identifier string.

        Raises:
            HTTPException: Identifier failed format or length validation.
        """
        try:
            return TreeNoteContracts.clean_id(value, field_name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _validate_optional_id(value: str | None, field_name: str) -> str | None:
        """Validate an optional query identifier or raise HTTP 422.

        Args:
            value: Optional raw identifier string.
            field_name: Logical parameter name for error reporting.

        Returns:
            Canonical validated identifier or ``None``.

        Raises:
            HTTPException: Non-null identifier failed validation.
        """
        try:
            return TreeNoteContracts.clean_optional_id(value, field_name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _validate_optional_date(value: str | None) -> str | None:
        """Validate an optional ISO calendar date or raise HTTP 422.

        Args:
            value: Optional ``YYYY-MM-DD`` query parameter.

        Returns:
            Canonical ``YYYY-MM-DD`` string or ``None``.

        Raises:
            HTTPException: Non-null date string is invalid.
        """
        if value is None:
            return None
        try:
            return TreeNoteContracts.clean_note_date(value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _parse_pagination(limit_raw: str, offset_raw: str) -> tuple[int, int]:
        """Parse and bound search pagination query parameters.

        Args:
            limit_raw: Unparsed query string for ``limit``.
            offset_raw: Unparsed query string for ``offset``.

        Returns:
            Validated ``(limit, offset)`` integer tuple.

        Raises:
            HTTPException: Non-integer or out-of-range pagination values.
        """
        try:
            limit = int(limit_raw)
            offset = int(offset_raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail='limit and offset must be integers.') from exc
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(status_code=422, detail='limit must be 1..100 and offset >= 0.')
        return limit, offset

    @staticmethod
    def _resolve_delete_node_request(
        request: Request,
        body: TreeNoteContracts.DeleteRequest | None,
    ) -> TreeNoteContracts.DeleteRequest:
        """Merge query and optional body delete parameters into a strict ``DeleteRequest``.

        Args:
            request: Incoming HTTP request supplying query fallback parameters.
            body: Optional JSON body containing delete CAS/recursive flags.

        Returns:
            Validated ``TreeNoteContracts.DeleteRequest``.

        Raises:
            HTTPException: Missing or malformed ``expected_basis_sha256``.
        """
        if body is not None:
            return body
        expected_basis = request.query_params.get('expected_basis_sha256')
        if expected_basis is None:
            raise HTTPException(status_code=422, detail='expected_basis_sha256 is required.')
        recursive = request.query_params.get('recursive', 'false').lower() == 'true'
        try:
            return TreeNoteContracts.DeleteRequest(expected_basis_sha256=expected_basis, recursive=recursive)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _resolve_delete_part_request(
        request: Request,
        body: TreeNotePartContracts.PartDeleteRequest | None,
    ) -> TreeNotePartContracts.PartDeleteRequest:
        """Merge query and optional body part delete parameters into a strict ``PartDeleteRequest``.

        Args:
            request: Incoming HTTP request supplying query fallback parameters.
            body: Optional JSON body containing part CAS basis digest.

        Returns:
            Validated ``TreeNotePartContracts.PartDeleteRequest``.

        Raises:
            HTTPException: Missing or malformed ``expected_part_basis_sha256``.
        """
        if body is not None:
            return body
        expected_part_basis = request.query_params.get('expected_part_basis_sha256')
        if expected_part_basis is None:
            raise HTTPException(status_code=422, detail='expected_part_basis_sha256 is required.')
        try:
            return TreeNotePartContracts.PartDeleteRequest(expected_part_basis_sha256=expected_part_basis)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
