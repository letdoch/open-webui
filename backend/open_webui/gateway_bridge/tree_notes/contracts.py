"""Strict Pydantic v2 core tree, node, calendar, and search contracts."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from open_webui.gateway_bridge.tree_notes.digest import (
    DATE_ISO_PATTERN,
    SHA256_HEX_PATTERN,
    TreeNoteDigest,
)


class TreeNoteContracts:
    """Namespace enclosing core node, tree, calendar, and search Pydantic v2 contracts."""

    MAX_SIBLINGS_PER_PARENT = 100_000
    MAX_TREE_DEPTH = 64

    class StatusResponse(BaseModel):
        """Runtime feature availability payload for client navigation."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        enabled: bool
        promote_enabled: bool

    class BasisConflictResponse(BaseModel):
        """Structured HTTP 409 Conflict payload returned on stale optimistic concurrency digests."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        error: Literal['basis_conflict'] = 'basis_conflict'
        node_id: str
        expected_basis_sha256: str | None = None
        current_basis_sha256: str
        part_id: str | None = None
        expected_part_basis_sha256: str | None = None
        current_part_basis_sha256: str | None = None
        current_updated_at: int

    class DomainErrorResponse(BaseModel):
        """Structured HTTP 409/422 domain guard error payload."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        error: str
        detail: str
        node_id: str | None = None

    class BreadcrumbItem(BaseModel):
        """One ancestor link along a node's path from its root."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        title: str
        node_kind: Literal['folder', 'note', 'daily_note', 'task_draft']

    class NodeSummaryDTO(BaseModel):
        """Compact node metadata projection used in trees, calendar, search, and child lists."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        parent_id: str | None
        sort_order: int
        title: str
        node_kind: Literal['folder', 'note', 'daily_note', 'task_draft']
        note_date: str | None = None
        archived_at: int | None
        created_at: int
        updated_at: int
        tags: list[str]
        child_count: int = 0

    class TreeNodeItem(BaseModel):
        """Recursive tree node entry for sidebar navigation."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node: TreeNoteContracts.NodeSummaryDTO
        children: list[TreeNoteContracts.TreeNodeItem]

    class TreeResponse(BaseModel):
        """Ordered hierarchical workspace tree response."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        items: list[TreeNoteContracts.TreeNodeItem]
        total_nodes: int

    class CalendarDayEntry(BaseModel):
        """One calendar date node and its nested workspace children."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        note_date: str
        node: TreeNoteContracts.NodeSummaryDTO
        children: list[TreeNoteContracts.TreeNodeItem]

    class CalendarMonthGroup(BaseModel):
        """Month bucket inside the projected YYYY / Month / DD calendar tree."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        month: int
        month_label: str
        days: list[TreeNoteContracts.CalendarDayEntry]

    class CalendarYearGroup(BaseModel):
        """Year bucket inside the projected YYYY / Month / DD calendar tree."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        year: int
        months: list[TreeNoteContracts.CalendarMonthGroup]

    class CalendarResponse(BaseModel):
        """Projected daily calendar hierarchy."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        years: list[TreeNoteContracts.CalendarYearGroup]
        total_days: int

    class SearchHit(BaseModel):
        """Single owner-scoped full-text or tag search match."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node: TreeNoteContracts.NodeSummaryDTO
        snippet: str
        matched_part_id: str | None = None
        matched_part_path: str | None = None
        breadcrumbs: list[TreeNoteContracts.BreadcrumbItem]

    class SearchResponse(BaseModel):
        """Paginated owner-scoped search response."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        items: list[TreeNoteContracts.SearchHit]
        total: int

    class CreateRequest(BaseModel):
        """Validated payload to create a folder, note, or task draft node."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        title: str
        node_kind: Literal['folder', 'note', 'task_draft'] = 'note'
        parent_id: str | None = None
        markdown: str = ''
        tags: list[str] = Field(default_factory=list)

        @field_validator('title', mode='after')
        @classmethod
        def _clean_title(cls, value: str) -> str:
            """Normalize and validate non-empty single-line node title.

            Args:
                value: Raw node title string.

            Returns:
                Normalized non-empty title string.
            """
            return TreeNoteContracts.clean_title(value)

        @field_validator('parent_id', mode='after')
        @classmethod
        def _clean_parent_id(cls, value: str | None) -> str | None:
            """Validate optional parent node identifier.

            Args:
                value: Optional raw parent node ID.

            Returns:
                Normalized parent node ID or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'parent_id')

        @field_validator('markdown', mode='after')
        @classmethod
        def _clean_markdown(cls, value: str) -> str:
            """Normalize newlines and Unicode NFC on initial Markdown content.

            Args:
                value: Raw Markdown text.

            Returns:
                Normalized LF Unicode NFC text.
            """
            return TreeNoteDigest.normalize_text(value)

        @field_validator('tags', mode='after')
        @classmethod
        def _clean_tags(cls, values: list[str]) -> list[str]:
            """Validate and deduplicate tag slugs.

            Args:
                values: Raw tag slug list.

            Returns:
                Deduplicated normalized tag slug list.
            """
            return TreeNoteContracts.clean_tag_list(values)

    class TodayRequest(BaseModel):
        """Optional IANA timezone selector for opening today's daily entry."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        tz: str | None = None

        @field_validator('tz', mode='after')
        @classmethod
        def _clean_tz(cls, value: str | None) -> str | None:
            """Trim optional IANA timezone name.

            Args:
                value: Optional raw timezone name.

            Returns:
                Validated single-line timezone string or ``None``.
            """
            if value is None:
                return None
            return TreeNoteDigest.normalize_single_line(value, 'tz', 64)

    class UpdateRequest(BaseModel):
        """CAS-protected metadata update request for a node."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_basis_sha256: str
        title: str | None = None
        node_kind: Literal['folder', 'note', 'task_draft'] | None = None

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate SHA-256 hex digest.

            Args:
                value: Raw SHA-256 hex digest string.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

        @field_validator('title', mode='after')
        @classmethod
        def _clean_title(cls, value: str | None) -> str | None:
            """Validate optional updated title.

            Args:
                value: Optional raw title string.

            Returns:
                Normalized title or ``None``.
            """
            return None if value is None else TreeNoteContracts.clean_title(value)

        @model_validator(mode='after')
        def _require_mutation(self) -> TreeNoteContracts.UpdateRequest:
            """Ensure at least one metadata field is supplied.

            Returns:
                Validated update request instance.
            """
            if self.title is None and self.node_kind is None:
                raise ValueError('At least one of title or node_kind must be provided.')
            return self

    class MarkdownPutRequest(BaseModel):
        """CAS-protected full Markdown document replacement request."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_basis_sha256: str
        markdown: str

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate SHA-256 hex digest.

            Args:
                value: Raw SHA-256 hex digest string.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

        @field_validator('markdown', mode='after')
        @classmethod
        def _clean_markdown(cls, value: str) -> str:
            """Normalize newlines and Unicode NFC on Markdown payload.

            Args:
                value: Raw Markdown body string.

            Returns:
                Normalized LF Unicode NFC text.
            """
            return TreeNoteDigest.normalize_text(value)

    class MoveRequest(BaseModel):
        """CAS-protected node reparenting and optional sibling positioning request."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_basis_sha256: str
        new_parent_id: str | None = None
        target_position: int | None = None

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate SHA-256 hex digest.

            Args:
                value: Raw SHA-256 hex digest string.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

        @field_validator('new_parent_id', mode='after')
        @classmethod
        def _clean_parent(cls, value: str | None) -> str | None:
            """Validate optional destination parent node ID.

            Args:
                value: Optional raw parent node ID.

            Returns:
                Validated identifier or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'new_parent_id')

        @field_validator('target_position', mode='after')
        @classmethod
        def _clean_pos(cls, value: int | None) -> int | None:
            """Validate bounded sibling position index.

            Args:
                value: Optional sibling index.

            Returns:
                Validated index or ``None``.
            """
            return TreeNoteContracts.clean_position(value)

    class ReorderRequest(BaseModel):
        """Sibling node reorder request protected by live sibling sequence expectation."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        parent_id: str | None = None
        ordered_node_ids: list[str]
        expected_sibling_ids: list[str]

        @field_validator('parent_id', mode='after')
        @classmethod
        def _clean_parent(cls, value: str | None) -> str | None:
            """Validate optional parent node ID.

            Args:
                value: Optional raw parent node ID.

            Returns:
                Validated identifier or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'parent_id')

        @field_validator('ordered_node_ids', 'expected_sibling_ids', mode='after')
        @classmethod
        def _clean_ids(cls, values: list[str]) -> list[str]:
            """Validate non-empty unique sibling ID lists.

            Args:
                values: Raw ordered sibling identifier list.

            Returns:
                Validated list of unique identifiers.
            """
            return TreeNoteContracts.clean_unique_id_list(values, 'sibling_ids')

        @model_validator(mode='after')
        def _verify_permutation(self) -> TreeNoteContracts.ReorderRequest:
            """Verify ordered_node_ids is a permutation of expected_sibling_ids.

            Returns:
                Validated reorder request instance.
            """
            if set(self.ordered_node_ids) != set(self.expected_sibling_ids):
                raise ValueError('ordered_node_ids must be a permutation of expected_sibling_ids.')
            return self

    class ArchiveRequest(BaseModel):
        """CAS-protected soft-archive or unarchive request."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_basis_sha256: str
        archived: bool

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate SHA-256 hex digest.

            Args:
                value: Raw SHA-256 hex digest string.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

    class DeleteRequest(BaseModel):
        """CAS-protected node or subtree deletion parameters."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_basis_sha256: str
        recursive: bool = False

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate SHA-256 hex digest.

            Args:
                value: Raw SHA-256 hex digest string.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

    class DeleteResponse(BaseModel):
        """Deleted node ID list returned by DELETE /nodes/{node_id}."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        deleted_node_ids: list[str]

    @staticmethod
    def clean_id(value: str, field_name: str) -> str:
        """Validate a required single-line resource identifier.

        Args:
            value: Raw identifier string.
            field_name: Field label for error messages.

        Returns:
            Normalized identifier string.

        Raises:
            ValueError: Identifier contains whitespace, control chars, or invalid length.
        """
        cleaned = TreeNoteDigest.normalize_single_line(value, field_name, 64)
        if any(ch.isspace() for ch in cleaned):
            raise ValueError(f'{field_name} must not contain whitespace.')
        return cleaned

    @classmethod
    def clean_optional_id(cls, value: str | None, field_name: str) -> str | None:
        """Validate an optional single-line resource identifier.

        Args:
            value: Raw optional identifier string.
            field_name: Field label for diagnostics.

        Returns:
            Normalized identifier or ``None``.
        """
        if value is None:
            return None
        return cls.clean_id(value, field_name)

    @classmethod
    def clean_unique_id_list(cls, values: list[str], field_name: str) -> list[str]:
        """Validate a bounded list of unique identifiers.

        Args:
            values: Raw identifier list.
            field_name: Field label for diagnostics.

        Returns:
            List of validated unique identifiers.

        Raises:
            ValueError: List exceeds bounds, contains duplicates, or has invalid IDs.
        """
        if len(values) > cls.MAX_SIBLINGS_PER_PARENT:
            raise ValueError(f'{field_name} exceeds {cls.MAX_SIBLINGS_PER_PARENT} items.')
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in values:
            item = cls.clean_id(raw, field_name)
            if item in seen:
                raise ValueError(f'{field_name} contains duplicate identifier: {item}')
            seen.add(item)
            cleaned.append(item)
        return cleaned

    @staticmethod
    def clean_title(value: str) -> str:
        """Normalize and validate a node title.

        Args:
            value: Raw title string.

        Returns:
            Trimmed NFC single-line title.
        """
        return TreeNoteDigest.normalize_single_line(value, 'title', 255)

    @staticmethod
    def clean_sha256(value: str, field_name: str) -> str:
        """Validate a lowercase 64-character SHA-256 hex digest.

        Args:
            value: Raw SHA-256 hex digest.
            field_name: Field label for diagnostics.

        Returns:
            Lowercase 64-hex digest string.

        Raises:
            ValueError: Value is not a valid 64-char lowercase hex SHA-256 digest.
        """
        normalized = value.strip()
        if SHA256_HEX_PATTERN.fullmatch(normalized) is None:
            raise ValueError(f'{field_name} must be a 64-character lowercase hex SHA-256 digest.')
        return normalized

    @staticmethod
    def clean_note_date(value: str) -> str:
        """Validate an ISO-8601 calendar date YYYY-MM-DD.

        Args:
            value: Raw date string.

        Returns:
            Canonical ``YYYY-MM-DD`` string.

        Raises:
            ValueError: Date string is malformed or not a real calendar date.
        """
        trimmed = value.strip()
        if DATE_ISO_PATTERN.fullmatch(trimmed) is None:
            raise ValueError('note_date must match YYYY-MM-DD.')
        dt.date.fromisoformat(trimmed)
        return trimmed

    @classmethod
    def clean_position(cls, value: int | None) -> int | None:
        """Validate an optional bounded sibling index.

        Args:
            value: Optional zero-based sibling index.

        Returns:
            Validated index or ``None``.

        Raises:
            ValueError: Position is negative or reaches the sibling limit.
        """
        if value is None:
            return None
        if not 0 <= value < cls.MAX_SIBLINGS_PER_PARENT:
            raise ValueError(f'position must be between 0 and {cls.MAX_SIBLINGS_PER_PARENT - 1}.')
        return value

    @staticmethod
    def clean_tag_list(values: list[str]) -> list[str]:
        """Validate and deduplicate a list of lowercase tag slugs.

        Args:
            values: Raw tag slug list.

        Returns:
            Deduplicated normalized tag slug list.

        Raises:
            ValueError: Tag list exceeds 100 tags or includes an invalid slug.
        """
        if len(values) > 100:
            raise ValueError('At most 100 tags per node are allowed.')
        seen: set[str] = set()
        result: list[str] = []
        for raw in values:
            slug = TreeNoteDigest.normalize_tag_slug(raw)
            if slug not in seen:
                seen.add(slug)
                result.append(slug)
        return result

    @classmethod
    def rebuild_models(cls) -> None:
        """Rebuild nested Pydantic models so forward references across sibling classes resolve."""
        for model_cls in (
            cls.StatusResponse,
            cls.BasisConflictResponse,
            cls.DomainErrorResponse,
            cls.BreadcrumbItem,
            cls.NodeSummaryDTO,
            cls.TreeNodeItem,
            cls.TreeResponse,
            cls.CalendarDayEntry,
            cls.CalendarMonthGroup,
            cls.CalendarYearGroup,
            cls.CalendarResponse,
            cls.SearchHit,
            cls.SearchResponse,
            cls.CreateRequest,
            cls.TodayRequest,
            cls.UpdateRequest,
            cls.MarkdownPutRequest,
            cls.MoveRequest,
            cls.ReorderRequest,
            cls.ArchiveRequest,
            cls.DeleteRequest,
            cls.DeleteResponse,
        ):
            model_cls.model_rebuild()


TreeNoteContracts.rebuild_models()
