"""Owner-scoped built-in chat assistant tools for hierarchical tree notes."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.repository import TreeNoteRepository
from open_webui.gateway_bridge.tree_notes.repository_queries import TreeNoteQueries
from open_webui.gateway_bridge.tree_notes.service import TreeNoteService
from open_webui.internal.db import get_async_db
from open_webui.models.users import UserModel, Users

log = logging.getLogger(__name__)


class TreeNoteTools:
    """Built-in chat assistant tool entrypoints with zero admin bypass."""

    class ToolUserProbe(BaseModel):
        """Strict Pydantic probe for Open WebUI's injected ``__user__`` context."""

        model_config = ConfigDict(extra='ignore')

        id: str = ''
        role: str = 'user'

    class CreateToolParams(BaseModel):
        """Input payload for ``create_tree_note``."""

        model_config = ConfigDict(extra='forbid')

        title: str
        markdown: str = ''
        parent_id: str | None = None
        node_kind: Literal['folder', 'note', 'task_draft'] = 'note'

    class UpdatePartToolParams(BaseModel):
        """Input payload for ``update_tree_note_part``."""

        model_config = ConfigDict(extra='forbid')

        node_id: str
        expected_basis_sha256: str | None = None
        part_id: str | None = None
        part_path: str | None = None
        expected_part_basis_sha256: str | None = None
        content_markdown: str | None = None
        title: str | None = None
        checked: bool | None = None
        full_markdown: str | None = None

    class _ToolErrorOutput(BaseModel):
        """Typed error response serialized back to the assistant."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        error: str
        code: str

    class _ToolSearchItem(BaseModel):
        """Compact search result item for assistant consumption."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        title: str
        node_kind: str
        snippet: str
        matched_part_id: str | None
        matched_part_path: str | None
        url: str
        updated_at: int

    class _ToolSearchOutput(BaseModel):
        """Typed list response for ``search_tree_notes``."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        items: list[TreeNoteTools._ToolSearchItem]

    class _ToolViewOutput(BaseModel):
        """Typed full-node view response for ``view_tree_note``."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        title: str
        node_kind: str
        basis_sha256: str
        markdown: str
        parts: list[TreeNotePartContracts.PartDTO]
        url: str

    class _ToolCreateOutput(BaseModel):
        """Typed creation confirmation payload."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        status: str
        id: str
        title: str
        basis_sha256: str
        url: str

    class _ToolUpdateOutput(BaseModel):
        """Typed update confirmation payload."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        status: str
        id: str
        title: str
        basis_sha256: str
        part_id: str | None = None
        part_basis_sha256: str | None = None
        url: str

    @staticmethod
    async def search_tree_notes(
        query: str,
        tag: str | None = None,
        limit: int = 10,
        __request__: Request | None = None,
        __user__: TreeNoteTools.ToolUserProbe | None = None,
    ) -> str:
        """Search the authenticated user's personal tree notes by text or tag.

        Args:
            query: Search query string.
            tag: Optional lowercase tag slug filter.
            limit: Maximum number of matching notes (1..50).
            __request__: Injected FastAPI request context.
            __user__: Injected authenticated user context.

        Returns:
            JSON string containing matching tree notes or an error object.
        """
        user, err = await TreeNoteTools._authorize_caller(__request__, __user__)
        if err is not None or user is None:
            return err or TreeNoteTools._err('unauthorized', 'User authorization failed.')
        try:
            bounded_limit = max(1, min(50, int(limit)))
            params = TreeNoteQueries.SearchQueryParams(query=query, tag=tag, limit=bounded_limit, offset=0)
            result = await TreeNoteQueries.search(user.id, params)
            items = [
                TreeNoteTools._ToolSearchItem(
                    id=hit.node.id,
                    title=hit.node.title,
                    node_kind=hit.node.node_kind,
                    snippet=hit.snippet,
                    matched_part_id=hit.matched_part_id,
                    matched_part_path=hit.matched_part_path,
                    url=f'/tree-notes/{hit.node.id}',
                    updated_at=hit.node.updated_at,
                )
                for hit in result.items
            ]
            return TreeNoteTools._ToolSearchOutput(items=items).model_dump_json()
        except (ValueError, ValidationError) as exc:
            return TreeNoteTools._err('validation_error', str(exc))
        except Exception as exc:
            log.warning('search_tree_notes failed: %s', type(exc).__name__)
            return TreeNoteTools._err('internal_error', 'Failed to search tree notes.')

    @staticmethod
    async def view_tree_note(
        node_id: str,
        __request__: Request | None = None,
        __user__: TreeNoteTools.ToolUserProbe | None = None,
    ) -> str:
        """Read a single owned tree note, its ``basis_sha256``, and addressable parts.

        Args:
            node_id: Target tree note node identifier.
            __request__: Injected FastAPI request context.
            __user__: Injected authenticated user context.

        Returns:
            JSON string with node metadata, Markdown, parts, and digests.
        """
        user, err = await TreeNoteTools._authorize_caller(__request__, __user__)
        if err is not None or user is None:
            return err or TreeNoteTools._err('unauthorized', 'User authorization failed.')
        try:
            clean_id = TreeNoteContracts.clean_id(node_id, 'node_id')
            node = await TreeNoteQueries.read_node(user.id, clean_id, include_parts=True)
            if node is None:
                return TreeNoteTools._err('not_found', 'Tree note not found.')
            return TreeNoteTools._ToolViewOutput(
                id=node.id,
                title=node.title,
                node_kind=node.node_kind,
                basis_sha256=node.basis_sha256,
                markdown=node.rendered_markdown,
                parts=node.parts,
                url=f'/tree-notes/{node.id}',
            ).model_dump_json()
        except ValueError as exc:
            return TreeNoteTools._err('validation_error', str(exc))
        except Exception as exc:
            log.warning('view_tree_note failed: %s', type(exc).__name__)
            return TreeNoteTools._err('internal_error', 'Failed to view tree note.')

    @staticmethod
    async def create_tree_note(
        payload: TreeNoteTools.CreateToolParams,
        __request__: Request | None = None,
        __user__: TreeNoteTools.ToolUserProbe | None = None,
    ) -> str:
        """Create a new personal folder, note, or task draft in the user's tree.

        Args:
            payload: Creation parameters (``title``, ``markdown``, ``parent_id``, ``node_kind``).
            __request__: Injected FastAPI request context.
            __user__: Injected authenticated user context.

        Returns:
            JSON string confirming creation and returning the new ``basis_sha256``.
        """
        user, err = await TreeNoteTools._authorize_caller(__request__, __user__)
        if err is not None or user is None:
            return err or TreeNoteTools._err('unauthorized', 'User authorization failed.')
        try:
            parsed = TreeNoteTools.CreateToolParams.model_validate(payload)
            req = TreeNoteContracts.CreateRequest(
                title=parsed.title,
                node_kind=parsed.node_kind,
                parent_id=parsed.parent_id,
                markdown=parsed.markdown,
                tags=[],
            )
            created = await TreeNoteService.create_node(user, req)
            return TreeNoteTools._ToolCreateOutput(
                status='success',
                id=created.id,
                title=created.title,
                basis_sha256=created.basis_sha256,
                url=f'/tree-notes/{created.id}',
            ).model_dump_json()
        except (ValueError, ValidationError) as exc:
            return TreeNoteTools._err('validation_error', str(exc))
        except TreeNoteRepository.RepoError as exc:
            return TreeNoteTools._err(exc.error_code, exc.detail)
        except HTTPException as exc:
            return TreeNoteTools._err('forbidden', str(exc.detail))
        except Exception as exc:
            log.warning('create_tree_note failed: %s', type(exc).__name__)
            return TreeNoteTools._err('internal_error', 'Failed to create tree note.')

    @staticmethod
    async def update_tree_note_part(
        payload: TreeNoteTools.UpdatePartToolParams,
        __request__: Request | None = None,
        __user__: TreeNoteTools.ToolUserProbe | None = None,
    ) -> str:
        """Update an addressable part (by UUID or ``part_path``) or replace full Markdown with CAS.

        Args:
            payload: Update parameters including node/part IDs and CAS basis digests.
            __request__: Injected FastAPI request context.
            __user__: Injected authenticated user context.

        Returns:
            JSON string with updated digests or conflict/validation diagnostics.
        """
        user, err = await TreeNoteTools._authorize_caller(__request__, __user__)
        if err is not None or user is None:
            return err or TreeNoteTools._err('unauthorized', 'User authorization failed.')
        try:
            parsed = TreeNoteTools.UpdatePartToolParams.model_validate(payload)
            return await TreeNoteTools._execute_update(user, parsed)
        except (ValueError, ValidationError) as exc:
            return TreeNoteTools._err('validation_error', str(exc))
        except TreeNoteRepository.RepoError as exc:
            if exc.conflict is not None:
                return exc.conflict.model_dump_json()
            return TreeNoteTools._err(exc.error_code, exc.detail)
        except HTTPException as exc:
            return TreeNoteTools._err('forbidden', str(exc.detail))
        except Exception as exc:
            log.warning('update_tree_note_part failed: %s', type(exc).__name__)
            return TreeNoteTools._err('internal_error', 'Failed to update tree note.')

    @classmethod
    async def _execute_update(
        cls,
        user: UserModel,
        parsed: TreeNoteTools.UpdatePartToolParams,
    ) -> str:
        """Dispatch full-Markdown replacement or targeted part update with strict CAS rules.

        Args:
            user: Verified owner ``UserModel``.
            parsed: Validated tool update parameters.

        Returns:
            Serialized JSON result envelope string.
        """
        clean_node_id = TreeNoteContracts.clean_id(parsed.node_id, 'node_id')
        if parsed.full_markdown is not None:
            if parsed.expected_basis_sha256 is None:
                return cls._err('cas_required', 'full_markdown updates require expected_basis_sha256.')
            md_req = TreeNoteContracts.MarkdownPutRequest(
                expected_basis_sha256=parsed.expected_basis_sha256,
                markdown=parsed.full_markdown,
            )
            updated = await TreeNoteService.put_markdown(user, clean_node_id, md_req)
            return cls._ToolUpdateOutput(
                status='success',
                id=updated.id,
                title=updated.title,
                basis_sha256=updated.basis_sha256,
                url=f'/tree-notes/{updated.id}',
            ).model_dump_json()

        node = await TreeNoteQueries.read_node(user.id, clean_node_id, include_parts=True)
        if node is None:
            return cls._err('not_found', 'Tree note not found.')
        target_part, basis_digest = cls._resolve_target_part(node, parsed)
        patch_req = TreeNotePartContracts.PartUpdateRequest(
            expected_part_basis_sha256=basis_digest,
            title=parsed.title,
            content_markdown=parsed.content_markdown,
            checked=parsed.checked,
        )
        mutated = await TreeNoteService.update_part(user, clean_node_id, target_part.id, patch_req)
        return cls._ToolUpdateOutput(
            status='success',
            id=mutated.node.id,
            title=mutated.node.title,
            basis_sha256=mutated.node.basis_sha256,
            part_id=mutated.part.id,
            part_basis_sha256=mutated.part.part_basis_sha256,
            url=f'/tree-notes/{mutated.node.id}',
        ).model_dump_json()

    @classmethod
    def _resolve_target_part(
        cls,
        node: TreeNotePartContracts.ReadResponse,
        parsed: TreeNoteTools.UpdatePartToolParams,
    ) -> tuple[TreeNotePartContracts.PartDTO, str]:
        """Resolve part by canonical ID or positional ``part_path`` under CAS constraints.

        Args:
            node: Loaded owner tree-note read response including ordered parts.
            parsed: Validated tool update parameters specifying target locator and CAS token.

        Returns:
            Tuple of ``(resolved_part, expected_part_basis_sha256)``.

        Raises:
            TreeNoteRepository.RepoError: Target part missing or whole-node CAS token mismatched.
            ValueError: Caller omitted both part locator or required CAS basis token.
        """
        if parsed.part_id is not None:
            part = next((item for item in node.parts if item.id == parsed.part_id), None)
            if part is None:
                raise TreeNoteRepository.RepoError(404, 'not_found', 'Tree note part not found.', node.id)
            if parsed.expected_part_basis_sha256 is not None:
                return part, parsed.expected_part_basis_sha256
            if parsed.expected_basis_sha256 is not None:
                if node.basis_sha256 != parsed.expected_basis_sha256:
                    raise TreeNoteRepository.RepoError(
                        409,
                        'basis_conflict',
                        'Node changed concurrently.',
                        node.id,
                        TreeNoteContracts.BasisConflictResponse(
                            node_id=node.id,
                            expected_basis_sha256=parsed.expected_basis_sha256,
                            current_basis_sha256=node.basis_sha256,
                            current_updated_at=node.updated_at,
                        ),
                    )
                return part, part.part_basis_sha256
            raise ValueError('expected_part_basis_sha256 or expected_basis_sha256 is required.')

        if parsed.part_path is not None:
            if parsed.expected_basis_sha256 is None:
                raise ValueError('Positional part_path edits require whole-node expected_basis_sha256.')
            if node.basis_sha256 != parsed.expected_basis_sha256:
                raise TreeNoteRepository.RepoError(
                    409,
                    'basis_conflict',
                    'Node changed concurrently.',
                    node.id,
                    TreeNoteContracts.BasisConflictResponse(
                        node_id=node.id,
                        expected_basis_sha256=parsed.expected_basis_sha256,
                        current_basis_sha256=node.basis_sha256,
                        current_updated_at=node.updated_at,
                    ),
                )
            part = next((item for item in node.parts if item.part_path == parsed.part_path), None)
            if part is None:
                raise TreeNoteRepository.RepoError(404, 'not_found', 'Tree note part_path not found.', node.id)
            return part, part.part_basis_sha256

        raise ValueError('Provide part_id, part_path, or full_markdown.')

    @classmethod
    async def _authorize_caller(
        cls,
        request: Request | None,
        raw_user: TreeNoteTools.ToolUserProbe | None,
    ) -> tuple[UserModel | None, str | None]:
        """Validate request, caller identity, feature toggles, and strict owner scoping.

        Args:
            request: Incoming Open WebUI FastAPI request context.
            raw_user: Validated caller identity probe from chat tool runtime.

        Returns:
            Tuple of ``(verified_user_or_none, serialized_error_or_none)``.
        """
        if request is None:
            return None, cls._err('missing_request', 'Request context is unavailable.')
        if raw_user is None:
            return None, cls._err('missing_user', 'User context is unavailable.')
        try:
            probe = cls.ToolUserProbe.model_validate(raw_user)
        except ValidationError:
            return None, cls._err('invalid_user', 'Invalid user context.')
        if not probe.id.strip():
            return None, cls._err('missing_user', 'User identifier is missing.')
        async with get_async_db() as session:
            user = await Users.get_user_by_id(probe.id, db=session)
        if user is None:
            return None, cls._err('unauthorized', 'User account not found.')
        try:
            await TreeNoteService.ensure_access(request, user)
        except HTTPException as exc:
            return None, cls._err('forbidden', str(exc.detail))
        return user, None

    @classmethod
    def _err(cls, code: str, message: str) -> str:
        """Serialize a typed error envelope via Pydantic.

        Args:
            code: Machine-readable domain error code.
            message: Human-readable explanation string.

        Returns:
            JSON string serialized from ``_ToolErrorOutput``.
        """
        return cls._ToolErrorOutput(error=message, code=code).model_dump_json()

    @classmethod
    def rebuild_models(cls) -> None:
        """Resolve nested forward references across tool output schemas."""
        cls._ToolSearchOutput.model_rebuild()


TreeNoteTools.rebuild_models()
