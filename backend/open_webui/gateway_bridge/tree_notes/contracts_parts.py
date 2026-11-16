"""Strict Pydantic v2 contracts for tree note parts, promotion, refs, and legacy import."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from open_webui.gateway_bridge.tree_notes.contracts import TreeNoteContracts
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest


class TreeNotePartContracts:
    """Namespace enclosing strict Pydantic v2 contracts for parts, refs, promote, and import."""

    class ExternalRefDTO(BaseModel):
        """External reference link attached to a tree note node."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        target_kind: Literal['project_idea', 'project_task', 'imported_note']
        external_id: str
        created_at: int

    class PartDTO(BaseModel):
        """Addressable Markdown block projection with computed digest and dotted path."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        id: str
        node_id: str
        parent_part_id: str | None
        position: int
        part_kind: Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']
        heading_level: int | None
        title: str | None
        content_markdown: str
        list_style: Literal['unordered', 'ordered'] | None
        language: str | None
        checked: bool | None
        updated_at: int
        part_basis_sha256: str
        part_path: str

    class PartCreateRequest(BaseModel):
        """Validated single-part insertion payload."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        parent_part_id: str | None = None
        position: int | None = None
        part_kind: Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']
        heading_level: int | None = None
        title: str | None = None
        content_markdown: str = ''
        list_style: Literal['unordered', 'ordered'] | None = None
        language: str | None = None
        checked: bool | None = None
        expected_basis_sha256: str | None = None
        expected_part_basis_sha256: str | None = None

        @field_validator('parent_part_id', mode='after')
        @classmethod
        def _clean_parent(cls, value: str | None) -> str | None:
            """Validate optional parent part ID.

            Args:
                value: Optional raw parent part ID.

            Returns:
                Validated identifier or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'parent_part_id')

        @field_validator('position', mode='after')
        @classmethod
        def _clean_pos(cls, value: int | None) -> int | None:
            """Validate bounded target position.

            Args:
                value: Optional sibling index.

            Returns:
                Validated position or ``None``.
            """
            return TreeNoteContracts.clean_position(value)

        @field_validator('expected_basis_sha256', 'expected_part_basis_sha256', mode='after')
        @classmethod
        def _clean_digests(cls, value: str | None) -> str | None:
            """Validate optional SHA-256 hex digest.

            Args:
                value: Optional raw SHA-256 hex string.

            Returns:
                Validated digest or ``None``.
            """
            return None if value is None else TreeNoteContracts.clean_sha256(value, 'expected_digest')

        @field_validator('title', mode='after')
        @classmethod
        def _clean_title(cls, value: str | None) -> str | None:
            """Normalize optional section heading title.

            Args:
                value: Optional raw heading title.

            Returns:
                Normalized single-line heading title or ``None``.
            """
            return None if value is None else TreeNoteDigest.normalize_single_line(value, 'title', 1000)

        @field_validator('content_markdown', mode='after')
        @classmethod
        def _clean_content(cls, value: str) -> str:
            """Normalize Markdown body newlines and NFC encoding.

            Args:
                value: Raw Markdown block body.

            Returns:
                Normalized LF Unicode NFC text.
            """
            return TreeNoteDigest.normalize_text(value)

        @field_validator('language', mode='after')
        @classmethod
        def _clean_lang(cls, value: str | None) -> str | None:
            """Validate optional fence info-string language.

            Args:
                value: Optional fence info string.

            Returns:
                Normalized language identifier or ``None``.
            """
            if value is None:
                return None
            return TreeNoteDigest.normalize_single_line(value, 'language', 64)

        @model_validator(mode='after')
        def _validate_kind_and_cas(self) -> TreeNotePartContracts.PartCreateRequest:
            """Enforce per-kind DB invariants and CAS digest requirements.

            Returns:
                Validated ``PartCreateRequest`` instance.
            """
            TreeNotePartContracts.validate_part_create_fields(self)
            return self

    class PartUpdateRequest(BaseModel):
        """CAS-protected partial update request for an existing part."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_part_basis_sha256: str
        heading_level: int | None = None
        title: str | None = None
        content_markdown: str | None = None
        list_style: Literal['unordered', 'ordered'] | None = None
        language: str | None = None
        clear_language: bool = False
        checked: bool | None = None
        clear_checked: bool = False

        @field_validator('expected_part_basis_sha256', mode='after')
        @classmethod
        def _clean_part_basis(cls, value: str) -> str:
            """Validate expected part SHA-256 hex digest.

            Args:
                value: Raw part CAS basis digest.

            Returns:
                Validated lowercase 64-hex SHA-256 digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_part_basis_sha256')

        @field_validator('title', mode='after')
        @classmethod
        def _clean_title(cls, value: str | None) -> str | None:
            """Normalize optional section heading title.

            Args:
                value: Optional raw heading title.

            Returns:
                Normalized single-line heading title or ``None``.
            """
            return None if value is None else TreeNoteDigest.normalize_single_line(value, 'title', 1000)

        @field_validator('content_markdown', mode='after')
        @classmethod
        def _clean_content(cls, value: str | None) -> str | None:
            """Normalize optional Markdown block content.

            Args:
                value: Optional raw Markdown body.

            Returns:
                Normalized Markdown text or ``None``.
            """
            return None if value is None else TreeNoteDigest.normalize_text(value)

        @field_validator('language', mode='after')
        @classmethod
        def _clean_lang(cls, value: str | None) -> str | None:
            """Validate optional fence info-string language.

            Args:
                value: Optional fence info string.

            Returns:
                Normalized language identifier or ``None``.
            """
            if value is None:
                return None
            return TreeNoteDigest.normalize_single_line(value, 'language', 64)

        @model_validator(mode='after')
        def _validate_updates(self) -> TreeNotePartContracts.PartUpdateRequest:
            """Validate heading level bounds and mutually exclusive clear flags.

            Returns:
                Validated ``PartUpdateRequest`` instance.
            """
            if self.heading_level is not None and not 1 <= self.heading_level <= 6:
                raise ValueError('heading_level must be between 1 and 6.')
            if self.clear_language and self.language is not None:
                raise ValueError('clear_language cannot be combined with language.')
            if self.clear_checked and self.checked is not None:
                raise ValueError('clear_checked cannot be combined with checked.')
            return self

    class PartReorderRequest(BaseModel):
        """CAS-protected part sibling reorder request within a node container."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        parent_part_id: str | None = None
        ordered_part_ids: list[str]
        expected_sibling_part_ids: list[str]
        expected_basis_sha256: str | None = None
        expected_part_basis_sha256: str | None = None

        @field_validator('parent_part_id', mode='after')
        @classmethod
        def _clean_parent(cls, value: str | None) -> str | None:
            """Validate optional parent part identifier.

            Args:
                value: Optional raw parent part ID.

            Returns:
                Validated identifier or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'parent_part_id')

        @field_validator('ordered_part_ids', 'expected_sibling_part_ids', mode='after')
        @classmethod
        def _clean_ids(cls, values: list[str]) -> list[str]:
            """Validate non-empty unique sibling part ID sequences.

            Args:
                values: Raw part identifier list.

            Returns:
                Validated unique part identifier list.
            """
            return TreeNoteContracts.clean_unique_id_list(values, 'sibling_part_ids')

        @field_validator('expected_basis_sha256', 'expected_part_basis_sha256', mode='after')
        @classmethod
        def _clean_digests(cls, value: str | None) -> str | None:
            """Validate optional SHA-256 hex digest.

            Args:
                value: Optional raw SHA-256 hex digest.

            Returns:
                Validated digest or ``None``.
            """
            return None if value is None else TreeNoteContracts.clean_sha256(value, 'expected_digest')

        @model_validator(mode='after')
        def _validate_cas_and_perm(self) -> TreeNotePartContracts.PartReorderRequest:
            """Ensure sibling permutation and CAS digest requirements hold.

            Returns:
                Validated ``PartReorderRequest`` instance.
            """
            if set(self.ordered_part_ids) != set(self.expected_sibling_part_ids):
                raise ValueError('ordered_part_ids must be a permutation of expected_sibling_part_ids.')
            if self.parent_part_id is None and self.expected_basis_sha256 is None:
                raise ValueError('Top-level part reorder requires expected_basis_sha256.')
            if (
                self.parent_part_id is not None
                and self.expected_part_basis_sha256 is None
                and self.expected_basis_sha256 is None
            ):
                raise ValueError('Nested part reorder requires expected_part_basis_sha256 or expected_basis_sha256.')
            return self

    class PartDeleteRequest(BaseModel):
        """CAS-protected part subtree deletion payload."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        expected_part_basis_sha256: str

        @field_validator('expected_part_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate part SHA-256 hex digest.

            Args:
                value: Raw part SHA-256 hex digest.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_part_basis_sha256')

    class ReadResponse(BaseModel):
        """Complete node projection including basis digest, Markdown, parts, tags, and refs."""

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
        basis_sha256: str
        rendered_markdown: str
        tags: list[str]
        breadcrumbs: list[TreeNoteContracts.BreadcrumbItem]
        parts: list[TreeNotePartContracts.PartDTO]
        external_refs: list[TreeNotePartContracts.ExternalRefDTO]
        children: list[TreeNoteContracts.NodeSummaryDTO]

    class DailyResponse(BaseModel):
        """Atomic daily calendar get-or-create response."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        created: bool
        note_date: str
        node: TreeNotePartContracts.ReadResponse

    class PartMutationResponse(BaseModel):
        """Response returned after creating or updating a single part."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node: TreeNotePartContracts.ReadResponse
        part: TreeNotePartContracts.PartDTO

    class PartDeleteResponse(BaseModel):
        """Response returned after deleting a part subtree."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        deleted_part_ids: list[str]
        node: TreeNotePartContracts.ReadResponse

    class TagsPutRequest(BaseModel):
        """Full replacement tag slug set for one node."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        tag_slugs: list[str]

        @field_validator('tag_slugs', mode='after')
        @classmethod
        def _clean_tags(cls, values: list[str]) -> list[str]:
            """Validate and deduplicate tag slugs.

            Args:
                values: Raw tag slug list.

            Returns:
                Deduplicated normalized tag slug list.
            """
            return TreeNoteContracts.clean_tag_list(values)

    class TagsResponse(BaseModel):
        """Updated tag list response for a node."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        node_id: str
        tag_slugs: list[str]

    class PromoteRequest(BaseModel):
        """Confirmed, idempotent, CAS-protected downstream promotion request."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        target_kind: Literal['project_idea', 'project_task']
        expected_basis_sha256: str
        confirmed: Literal[True]
        idempotency_key: str
        project_id: str | None = None
        summary: str | None = None

        @field_validator('expected_basis_sha256', mode='after')
        @classmethod
        def _clean_basis(cls, value: str) -> str:
            """Validate node SHA-256 hex digest.

            Args:
                value: Raw node basis SHA-256 hex digest.

            Returns:
                Validated lowercase 64-hex digest.
            """
            return TreeNoteContracts.clean_sha256(value, 'expected_basis_sha256')

        @field_validator('idempotency_key', mode='after')
        @classmethod
        def _clean_key(cls, value: str) -> str:
            """Validate non-empty single-line idempotency key.

            Args:
                value: Raw client idempotency key.

            Returns:
                Normalized non-empty key string.
            """
            return TreeNoteDigest.normalize_single_line(value, 'idempotency_key', 128)

        @field_validator('project_id', mode='after')
        @classmethod
        def _clean_project(cls, value: str | None) -> str | None:
            """Validate optional downstream project identifier.

            Args:
                value: Optional raw project ID.

            Returns:
                Validated project identifier or ``None``.
            """
            return TreeNoteContracts.clean_optional_id(value, 'project_id')

        @field_validator('summary', mode='after')
        @classmethod
        def _clean_summary(cls, value: str | None) -> str | None:
            """Normalize optional promotion summary text.

            Args:
                value: Optional summary text.

            Returns:
                Trimmed summary text or ``None``.
            """
            if value is None:
                return None
            trimmed = TreeNoteDigest.normalize_text(value).strip()
            return trimmed if trimmed else None

    class PromoteResponse(BaseModel):
        """Verified downstream promotion result and recorded external ref."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        ref: TreeNotePartContracts.ExternalRefDTO
        target_url: str | None = None
        status: str

    class DeleteRefResponse(BaseModel):
        """Deleted external reference identifier response."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        deleted_ref_id: str

    class ImportLegacyRequest(BaseModel):
        """Additive idempotent legacy notes import request."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        note_ids: list[str] | None = None
        dry_run: bool = False

        @field_validator('note_ids', mode='after')
        @classmethod
        def _clean_note_ids(cls, values: list[str] | None) -> list[str] | None:
            """Validate optional list of legacy note IDs.

            Args:
                values: Optional raw legacy note ID list.

            Returns:
                Validated unique ID list or ``None``.
            """
            if values is None:
                return None
            return TreeNoteContracts.clean_unique_id_list(values, 'note_ids')

    class ImportLegacyItemResult(BaseModel):
        """Outcome for a single legacy note processed during import."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        legacy_note_id: str
        title: str
        status: Literal['imported', 'skipped_existing', 'would_import', 'failed']
        node_id: str | None = None
        page_count: int = 0
        error: str | None = None

    class ImportLegacyResponse(BaseModel):
        """Summary and per-note outcomes of legacy note import."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        dry_run: bool
        imported_count: int
        skipped_count: int
        failed_count: int
        items: list[TreeNotePartContracts.ImportLegacyItemResult]

    @staticmethod
    def validate_part_create_fields(req: TreeNotePartContracts.PartCreateRequest) -> None:
        """Validate per-kind field constraints and CAS requirements for part creation.

        Args:
            req: Parsed ``PartCreateRequest`` instance.

        Raises:
            ValueError: Required per-kind fields, disallowed fields, or CAS digests are invalid.
        """
        TreeNotePartContracts._check_part_cas(req)
        TreeNotePartContracts._check_part_kind_attrs(req)

    @staticmethod
    def _check_part_cas(req: TreeNotePartContracts.PartCreateRequest) -> None:
        """Validate CAS digest presence based on top-level vs nested insertion.

        Args:
            req: Validated ``PartCreateRequest`` instance.

        Raises:
            ValueError: Required CAS digest for top-level or nested insertion is missing.
        """
        if req.parent_part_id is None and req.expected_basis_sha256 is None:
            raise ValueError('Top-level part creation requires expected_basis_sha256.')
        if (
            req.parent_part_id is not None
            and req.expected_part_basis_sha256 is None
            and req.expected_basis_sha256 is None
        ):
            raise ValueError('Nested part creation requires expected_part_basis_sha256 or expected_basis_sha256.')

    @staticmethod
    def _check_part_kind_attrs(req: TreeNotePartContracts.PartCreateRequest) -> None:
        """Enforce the closed per-kind column contract on part creation.

        Args:
            req: Validated ``PartCreateRequest`` instance.
        """
        TreeNotePartContracts._check_section_and_list_attrs(req)
        TreeNotePartContracts._check_leaf_and_content_attrs(req)

    @staticmethod
    def _check_section_and_list_attrs(req: TreeNotePartContracts.PartCreateRequest) -> None:
        """Enforce section and list kind field invariants.

        Args:
            req: Validated ``PartCreateRequest`` instance.

        Raises:
            ValueError: Section heading/title or list_style constraints are violated.
        """
        kind = req.part_kind
        if kind == 'section':
            if req.heading_level is None or not 1 <= req.heading_level <= 6 or req.title is None:
                raise ValueError('section requires heading_level (1..6) and non-empty title.')
        elif req.heading_level is not None or req.title is not None:
            raise ValueError('heading_level and title are allowed only on section parts.')
        if kind == 'list' and req.list_style is None:
            raise ValueError('list requires list_style (unordered or ordered).')
        if kind != 'list' and req.list_style is not None:
            raise ValueError('list_style is allowed only on list parts.')

    @staticmethod
    def _check_leaf_and_content_attrs(req: TreeNotePartContracts.PartCreateRequest) -> None:
        """Enforce fence/item flags and content_markdown requirements.

        Args:
            req: Validated ``PartCreateRequest`` instance.

        Raises:
            ValueError: Disallowed fields or empty/non-empty content constraints fail.
        """
        kind = req.part_kind
        if kind != 'fence' and req.language is not None:
            raise ValueError('language is allowed only on fence parts.')
        if kind != 'item' and req.checked is not None:
            raise ValueError('checked is allowed only on item parts.')
        if kind == 'item' and req.parent_part_id is None:
            raise ValueError('item parts must specify parent_part_id.')
        if kind in ('section', 'list', 'rule') and req.content_markdown != '':
            raise ValueError(f'{kind} parts must have empty content_markdown.')
        if kind in ('paragraph', 'table') and not req.content_markdown.strip():
            raise ValueError(f'{kind} parts require non-empty content_markdown.')

    @classmethod
    def rebuild_models(cls) -> None:
        """Rebuild nested Pydantic models so forward references across sibling classes resolve."""
        for model_cls in (
            cls.ExternalRefDTO,
            cls.PartDTO,
            cls.PartCreateRequest,
            cls.PartUpdateRequest,
            cls.PartReorderRequest,
            cls.PartDeleteRequest,
            cls.ReadResponse,
            cls.DailyResponse,
            cls.PartMutationResponse,
            cls.PartDeleteResponse,
            cls.TagsPutRequest,
            cls.TagsResponse,
            cls.PromoteRequest,
            cls.PromoteResponse,
            cls.DeleteRefResponse,
            cls.ImportLegacyRequest,
            cls.ImportLegacyItemResult,
            cls.ImportLegacyResponse,
        ):
            model_cls.model_rebuild()


TreeNotePartContracts.rebuild_models()
