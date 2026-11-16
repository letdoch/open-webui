"""Deterministic Merkle digest, UUIDv7, and text normalization helpers for tree notes."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import unicodedata
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from open_webui.env import DATABASE_SCHEMA

if TYPE_CHECKING:
    from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
    from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart

TAG_SLUG_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}\Z')
TAG_DISALLOWED_CHARS_PATTERN = re.compile(r'[^a-z0-9_-]+')
SHA256_HEX_PATTERN = re.compile(r'^[0-9a-f]{64}\Z')
DATE_ISO_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}\Z')
CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x1f\x7f]')


class TreeNoteDigest:
    """Pure deterministic digest, identifier, and Unicode normalization helpers."""

    _uuid_lock = threading.Lock()
    _last_ms = 0
    _last_seq = 0

    class CanonicalPartDigestInput(BaseModel):
        """Alphabetically ordered canonical part payload for SHA-256 hashing."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        checked: bool | None
        children: tuple[str, ...]
        content_markdown: str
        heading_level: int | None
        language: str | None
        list_style: Literal['unordered', 'ordered'] | None
        part_kind: Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']
        title: str | None

    class CanonicalNodeDigestInput(BaseModel):
        """Alphabetically ordered canonical node payload for SHA-256 hashing."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        archived_at: int | None
        node_kind: Literal['folder', 'note', 'daily_note', 'task_draft']
        parent_id: str | None
        parts: tuple[str, ...]
        title: str

    @staticmethod
    def col_ref(table_col: str) -> str:
        """Resolve a schema-qualified foreign-key target column reference.

        Args:
            table_col: Unqualified ``table.column`` target string.

        Returns:
            Schema-prefixed target when ``DATABASE_SCHEMA`` is configured, else ``table_col``.
        """
        return f'{DATABASE_SCHEMA}.{table_col}' if DATABASE_SCHEMA else table_col

    @classmethod
    def new_uuid7(cls) -> str:
        """Generate a lowercase RFC 9562 UUIDv7 string with monotonic millisecond ordering.

        Returns:
            Canonical 36-character lowercase UUIDv7 string.
        """
        with cls._uuid_lock:
            now_ms = time.time_ns() // 1_000_000
            if now_ms > cls._last_ms:
                cls._last_ms = now_ms
                cls._last_seq = int.from_bytes(os.urandom(2), 'big') & 0x0FFF
            else:
                cls._last_seq = (cls._last_seq + 1) & 0x0FFF
                if cls._last_seq == 0:
                    cls._last_ms += 1
            ts_ms = cls._last_ms
            seq = cls._last_seq
        rand_b = int.from_bytes(os.urandom(8), 'big') & ((1 << 62) - 1)
        value = ((ts_ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (seq << 64) | (0b10 << 62) | rand_b
        return str(uuid.UUID(int=value))

    @staticmethod
    def normalize_text(value: str) -> str:
        """Normalize newlines to LF and apply Unicode NFC normalization.

        Args:
            value: Raw text string.

        Returns:
            Normalized multiline text string.
        """
        lf = value.replace('\r\n', '\n').replace('\r', '\n')
        return unicodedata.normalize('NFC', lf)

    @classmethod
    def normalize_single_line(cls, value: str, field_name: str, max_length: int) -> str:
        """Normalize and validate a non-empty single-line trimmed NFC string.

        Args:
            value: Raw caller text value.
            field_name: Field label for validation diagnostics.
            max_length: Maximum permitted character count.

        Returns:
            Trimmed single-line NFC string.

        Raises:
            ValueError: The value contains control characters, is empty, or exceeds ``max_length``.
        """
        normalized = cls.normalize_text(value)
        if CONTROL_CHAR_PATTERN.search(normalized):
            raise ValueError(f'{field_name} must not contain control characters or newlines.')
        trimmed = normalized.strip()
        if not trimmed:
            raise ValueError(f'{field_name} must not be empty.')
        if len(trimmed) > max_length:
            raise ValueError(f'{field_name} must be at most {max_length} characters.')
        return trimmed

    @classmethod
    def normalize_tag_slug(cls, value: str) -> str:
        """Validate and normalize a strict lowercase tag slug.

        Args:
            value: Caller-provided tag slug string.

        Returns:
            Normalized lowercase slug matching ``^[a-z0-9][a-z0-9_-]{0,63}$``.

        Raises:
            ValueError: The tag slug fails length or character restrictions.
        """
        normalized = cls.normalize_text(value).strip().lower()
        if TAG_SLUG_PATTERN.fullmatch(normalized) is None:
            raise ValueError('tag_slug must match ^[a-z0-9][a-z0-9_-]{0,63}$.')
        return normalized

    @classmethod
    def coerce_legacy_tag_slug(cls, value: str) -> str | None:
        """Coerce an arbitrary legacy tag label into a valid slug or return ``None``.

        Args:
            value: Raw legacy tag string.

        Returns:
            Canonical tag slug, or ``None`` if no valid slug characters remain.
        """
        lowered = cls.normalize_text(value).strip().lower()
        collapsed = TAG_DISALLOWED_CHARS_PATTERN.sub('-', lowered).strip('-_')
        if not collapsed:
            return None
        truncated = collapsed[:64].rstrip('-_')
        if TAG_SLUG_PATTERN.fullmatch(truncated) is None:
            return None
        return truncated

    @staticmethod
    def coerce_checked(value: bool | int | None) -> bool | None:
        """Coerce SQLite integer flags or booleans strictly to ``bool | None``.

        Args:
            value: Database ``checked`` column value.

        Returns:
            Strict boolean or ``None``.
        """
        if value is None:
            return None
        return bool(value)

    @staticmethod
    def next_monotonic_ns(current_updated_at: int, created_at: int) -> int:
        """Compute a monotonic non-decreasing UTC nanosecond timestamp.

        Args:
            current_updated_at: Existing node ``updated_at`` value.
            created_at: Existing node ``created_at`` value.

        Returns:
            Monotonic UTC Unix epoch nanoseconds timestamp.
        """
        return max(time.time_ns(), current_updated_at, created_at)

    @staticmethod
    def narrow_part_kind(
        value: str,
    ) -> Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']:
        """Narrow a persisted part_kind string into its closed literal union.

        Args:
            value: Persisted part_kind value from ``TreeNotePart``.

        Returns:
            Validated literal part kind.

        Raises:
            ValueError: Stored ``part_kind`` is not a recognized block type.
        """
        mapping: dict[str, Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']] = {
            'section': 'section',
            'paragraph': 'paragraph',
            'list': 'list',
            'item': 'item',
            'fence': 'fence',
            'table': 'table',
            'quote': 'quote',
            'rule': 'rule',
        }
        resolved = mapping.get(value)
        if resolved is None:
            raise ValueError(f'Invalid tree_note_part.part_kind: {value!r}')
        return resolved

    @staticmethod
    def narrow_list_style(value: str | None) -> Literal['unordered', 'ordered'] | None:
        """Narrow a persisted optional list_style string into its closed literal union.

        Args:
            value: Persisted ``list_style`` value from ``TreeNotePart``.

        Returns:
            Validated literal list style or ``None``.

        Raises:
            ValueError: Stored ``list_style`` is not recognized.
        """
        if value is None:
            return None
        if value == 'unordered':
            return 'unordered'
        if value == 'ordered':
            return 'ordered'
        raise ValueError(f'Invalid tree_note_part.list_style: {value!r}')

    @staticmethod
    def narrow_node_kind(value: str) -> Literal['folder', 'note', 'daily_note', 'task_draft']:
        """Narrow a persisted node_kind string into its closed literal union.

        Args:
            value: Persisted ``node_kind`` value from ``TreeNoteNode``.

        Returns:
            Validated literal node kind.

        Raises:
            ValueError: Stored ``node_kind`` is not recognized.
        """
        mapping: dict[str, Literal['folder', 'note', 'daily_note', 'task_draft']] = {
            'folder': 'folder',
            'note': 'note',
            'daily_note': 'daily_note',
            'task_draft': 'task_draft',
        }
        resolved = mapping.get(value)
        if resolved is None:
            raise ValueError(f'Invalid tree_note_node.node_kind: {value!r}')
        return resolved

    @staticmethod
    def narrow_target_kind(value: str) -> Literal['project_idea', 'project_task', 'imported_note']:
        """Narrow a persisted external-ref target_kind string into its closed literal union.

        Args:
            value: Persisted ``target_kind`` value from ``TreeNoteExternalRef``.

        Returns:
            Validated literal target kind.

        Raises:
            ValueError: Stored ``target_kind`` is not recognized.
        """
        mapping: dict[str, Literal['project_idea', 'project_task', 'imported_note']] = {
            'project_idea': 'project_idea',
            'project_task': 'project_task',
            'imported_note': 'imported_note',
        }
        resolved = mapping.get(value)
        if resolved is None:
            raise ValueError(f'Invalid tree_note_external_ref.target_kind: {value!r}')
        return resolved

    @classmethod
    def compute_part_digest(cls, part: TreeNotePart, child_digests: tuple[str, ...]) -> str:
        """Compute deterministic SHA-256 digest for one part given ordered child digests.

        Args:
            part: Loaded ``TreeNotePart`` ORM row.
            child_digests: Ordered SHA-256 digests of direct child parts.

        Returns:
            Lowercase 64-hex SHA-256 digest string.
        """
        canonical = cls.CanonicalPartDigestInput(
            checked=cls.coerce_checked(part.checked),
            children=child_digests,
            content_markdown=cls.normalize_text(part.content_markdown),
            heading_level=part.heading_level,
            language=cls.normalize_text(part.language) if part.language is not None else None,
            list_style=cls.narrow_list_style(part.list_style),
            part_kind=cls.narrow_part_kind(part.part_kind),
            title=cls.normalize_text(part.title) if part.title is not None else None,
        )
        return hashlib.sha256(canonical.model_dump_json().encode('utf-8')).hexdigest()

    @classmethod
    def compute_part_tree_digests(cls, parts: list[TreeNotePart]) -> tuple[dict[str, str], tuple[str, ...]]:
        """Compute bottom-up Merkle digests for every part and ordered top-level roots.

        Args:
            parts: All ``TreeNotePart`` rows belonging to one node.

        Returns:
            Tuple of ``(part_digests_by_id, top_level_part_digests)``.
        """
        children_map: dict[str | None, list[TreeNotePart]] = {}
        for part in sorted(parts, key=lambda row: (row.position, row.id)):
            children_map.setdefault(part.parent_part_id, []).append(part)
        digests: dict[str, str] = {}
        visiting: set[str] = set()

        def visit(part: TreeNotePart) -> str:
            """Recursively compute and memoize SHA-256 digest for ``part``."""
            if part.id in digests:
                return digests[part.id]
            if part.id in visiting:
                raise ValueError(f'Cycle detected in part tree at {part.id}')
            visiting.add(part.id)
            child_list = children_map.get(part.id) if part.id in children_map else []
            child_hashes = tuple(visit(child) for child in child_list)
            digest = cls.compute_part_digest(part, child_hashes)
            digests[part.id] = digest
            visiting.remove(part.id)
            return digest

        roots = children_map.get(None) if None in children_map else []
        top_digests = tuple(visit(root) for root in roots)
        return digests, top_digests

    @classmethod
    def compute_node_digest(cls, node: TreeNoteNode, parts: list[TreeNotePart]) -> tuple[str, dict[str, str]]:
        """Compute deterministic Merkle SHA-256 digest for a node and all its parts.

        Args:
            node: Loaded ``TreeNoteNode`` ORM row.
            parts: All ``TreeNotePart`` rows owned by ``node``.

        Returns:
            Tuple of ``(node_basis_sha256, part_digests_by_id)``.
        """
        part_digests, top_digests = cls.compute_part_tree_digests(parts)
        canonical = cls.CanonicalNodeDigestInput(
            archived_at=node.archived_at,
            node_kind=cls.narrow_node_kind(node.node_kind),
            parent_id=node.parent_id,
            parts=top_digests,
            title=cls.normalize_text(node.title),
        )
        node_digest = hashlib.sha256(canonical.model_dump_json().encode('utf-8')).hexdigest()
        return node_digest, part_digests

    @staticmethod
    def compute_part_paths(parts: list[TreeNotePart]) -> dict[str, str]:
        """Compute derived dotted relative addresses such as ``section_2.list_1.item_5``.

        Args:
            parts: All ``TreeNotePart`` rows belonging to one node.

        Returns:
            Mapping from ``part.id`` to its deterministic dotted ``part_path``.
        """
        children_map: dict[str | None, list[TreeNotePart]] = {}
        for part in sorted(parts, key=lambda row: (row.position, row.id)):
            children_map.setdefault(part.parent_part_id, []).append(part)
        paths: dict[str, str] = {}

        def assign(parent_id: str | None, prefix: str) -> None:
            """Assign deterministic dotted path addresses recursively."""
            siblings = children_map.get(parent_id) if parent_id in children_map else []
            kind_counts: dict[str, int] = {}
            for child in siblings:
                next_ord = (kind_counts[child.part_kind] + 1) if child.part_kind in kind_counts else 1
                kind_counts[child.part_kind] = next_ord
                segment = f'{child.part_kind}_{next_ord}'
                full_path = f'{prefix}.{segment}' if prefix else segment
                paths[child.id] = full_path
                assign(child.id, full_path)

        assign(None, '')
        return paths
