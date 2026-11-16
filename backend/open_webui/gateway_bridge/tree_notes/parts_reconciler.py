"""Markdown rendering and UUID-preserving part reconciliation helpers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart

PartKind = Literal['section', 'paragraph', 'list', 'item', 'fence', 'table', 'quote', 'rule']
ListStyle = Literal['unordered', 'ordered']


class TreeNotePartsReconciler:
    """Render ``TreeNotePart`` trees back to Markdown and reconcile UUIDs across edits."""

    class ParsedPart(BaseModel):
        """Canonical in-memory block representation emitted by the Markdown parser."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        temp_id: str
        parent_temp_id: str | None
        position: int
        part_kind: PartKind
        heading_level: int | None = None
        title: str | None = None
        content_markdown: str = ''
        list_style: ListStyle | None = None
        language: str | None = None
        checked: bool | None = None

    class ReconciledPart(BaseModel):
        """Resolved persistence block carrying a preserved or newly minted UUIDv7."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        part_id: str
        parent_part_id: str | None
        is_new: bool
        position: int
        part_kind: PartKind
        heading_level: int | None
        title: str | None
        content_markdown: str
        list_style: ListStyle | None
        language: str | None
        checked: bool | None

    class _ReconcileState(BaseModel):
        """Mutable reconciliation context passed across tree levels."""

        model_config = ConfigDict(strict=True, arbitrary_types_allowed=True, extra='forbid')

        parsed_by_parent: dict[str | None, list[TreeNotePartsReconciler.ParsedPart]]
        old_by_parent: dict[str | None, list[TreeNotePart]]
        used_old_ids: set[str] = set()
        reconciled: list[TreeNotePartsReconciler.ReconciledPart] = []

    @classmethod
    def render_markdown(cls, parts: list[TreeNotePart]) -> str:
        """Render an ordered ORM ``TreeNotePart`` tree back into canonical Markdown.

        Args:
            parts: All ``TreeNotePart`` rows belonging to one node.

        Returns:
            Canonical Markdown text using LF newlines.
        """
        if not parts:
            return ''
        nodes = [
            cls.ParsedPart(
                temp_id=part.id,
                parent_temp_id=part.parent_part_id,
                position=part.position,
                part_kind=TreeNoteDigest.narrow_part_kind(part.part_kind),
                heading_level=part.heading_level,
                title=part.title,
                content_markdown=TreeNoteDigest.normalize_text(part.content_markdown),
                list_style=TreeNoteDigest.narrow_list_style(part.list_style),
                language=part.language,
                checked=TreeNoteDigest.coerce_checked(part.checked),
            )
            for part in sorted(parts, key=lambda row: (row.position, row.id))
        ]
        return cls.render_parsed_parts(nodes)

    @classmethod
    def render_parsed_parts(cls, parts: list[TreeNotePartsReconciler.ParsedPart]) -> str:
        """Render a list of ``ParsedPart`` blocks into canonical Markdown.

        Args:
            parts: Parsed block tree list.

        Returns:
            Canonical Markdown document string.
        """
        if not parts:
            return ''
        children_map: dict[str | None, list[TreeNotePartsReconciler.ParsedPart]] = {}
        for part in sorted(parts, key=lambda row: (row.position, row.temp_id)):
            children_map.setdefault(part.parent_temp_id, []).append(part)
        roots = children_map.get(None) if None in children_map else []
        rendered = [cls._render_node(root, children_map) for root in roots]
        return '\n\n'.join(chunk for chunk in rendered if chunk)

    @classmethod
    def _render_node(
        cls,
        part: TreeNotePartsReconciler.ParsedPart,
        children_map: dict[str | None, list[TreeNotePartsReconciler.ParsedPart]],
    ) -> str:
        """Render one block and its children to canonical Markdown.

        Args:
            part: Target block descriptor to render.
            children_map: Grouping from parent ``temp_id`` to ordered child blocks.

        Returns:
            Rendered Markdown fragment for ``part`` and its subtree.
        """
        children = children_map.get(part.temp_id) if part.temp_id in children_map else []
        if part.part_kind == 'section':
            heading = f'{"#" * (part.heading_level or 1)} {part.title or ""}'.strip()
            body = [cls._render_node(child, children_map) for child in children]
            return '\n\n'.join([heading, *[chunk for chunk in body if chunk]])
        if part.part_kind in ('paragraph', 'table'):
            return part.content_markdown
        if part.part_kind == 'rule':
            return '---'
        if part.part_kind == 'fence':
            marker = '~~~~' if '```' in part.content_markdown else '```'
            lang = part.language or ''
            return f'{marker}{lang}\n{part.content_markdown}\n{marker}'
        if part.part_kind == 'quote':
            inner = (
                '\n\n'.join(cls._render_node(child, children_map) for child in children)
                if children
                else part.content_markdown
            )
            return '\n'.join(f'> {line}' if line else '>' for line in inner.split('\n'))
        if part.part_kind == 'list':
            return cls._render_list(part.list_style or 'unordered', children, children_map)
        return part.content_markdown

    @classmethod
    def _render_list(
        cls,
        style: ListStyle,
        items: list[TreeNotePartsReconciler.ParsedPart],
        children_map: dict[str | None, list[TreeNotePartsReconciler.ParsedPart]],
    ) -> str:
        """Render a list container and its nested items cleanly.

        Args:
            style: List bullet or ordered style.
            items: Direct child ``item`` blocks belonging to the list.
            children_map: Grouping from parent ``temp_id`` to child blocks.

        Returns:
            Markdown list block string.
        """
        rendered_items: list[str] = []
        for idx, item in enumerate(items):
            prefix = f'{idx + 1}. ' if style == 'ordered' else '- '
            checkbox = ''
            if item.checked is True:
                checkbox = '[x] '
            elif item.checked is False:
                checkbox = '[ ] '
            lead = f'{prefix}{checkbox}{item.content_markdown}'.rstrip()
            item_children = children_map.get(item.temp_id) if item.temp_id in children_map else []
            if not item_children:
                rendered_items.append(lead)
                continue
            indent = ' ' * len(prefix)
            sub_blocks = '\n\n'.join(cls._render_node(child, children_map) for child in item_children)
            indented = '\n'.join(f'{indent}{line}' if line else '' for line in sub_blocks.split('\n'))
            rendered_items.append(f'{lead}\n{indented}')
        return '\n'.join(rendered_items)

    @classmethod
    def reconcile_parts(
        cls,
        existing_parts: list[TreeNotePart],
        parsed_parts: list[TreeNotePartsReconciler.ParsedPart],
    ) -> tuple[list[TreeNotePartsReconciler.ReconciledPart], list[str]]:
        """Match parsed blocks against existing ORM parts to preserve UUIDs on edit.

        Args:
            existing_parts: Live ``TreeNotePart`` rows sorted by ``(position, id)``.
            parsed_parts: Newly parsed top-down ``ParsedPart`` blocks.

        Returns:
            Tuple of ``(reconciled_parts, deleted_part_ids_bottom_up)``.
        """
        old_by_parent: dict[str | None, list[TreeNotePart]] = {}
        for old in sorted(existing_parts, key=lambda row: (row.position, row.id)):
            old_by_parent.setdefault(old.parent_part_id, []).append(old)
        parsed_by_parent: dict[str | None, list[TreeNotePartsReconciler.ParsedPart]] = {}
        for part in sorted(parsed_parts, key=lambda row: (row.position, row.temp_id)):
            parsed_by_parent.setdefault(part.parent_temp_id, []).append(part)

        state = cls._ReconcileState(parsed_by_parent=parsed_by_parent, old_by_parent=old_by_parent)
        cls._reconcile_level(None, None, state)
        deleted_ids = cls._bottom_up_deleted_ids(existing_parts, state.used_old_ids)
        return state.reconciled, deleted_ids

    @classmethod
    def _reconcile_level(
        cls,
        parent_temp_id: str | None,
        parent_real_id: str | None,
        state: TreeNotePartsReconciler._ReconcileState,
    ) -> None:
        """Reconcile one parent container's children and recurse top-down.

        Args:
            parent_temp_id: Parser temporary parent identifier or ``None``.
            parent_real_id: Persisted UUIDv7 parent identifier or ``None``.
            state: Mutable reconciliation traversal state.
        """
        new_siblings = state.parsed_by_parent[parent_temp_id] if parent_temp_id in state.parsed_by_parent else []
        old_siblings = state.old_by_parent[parent_real_id] if parent_real_id in state.old_by_parent else []
        matched = cls._match_sibling_ids(new_siblings, old_siblings, state.used_old_ids)
        for part in new_siblings:
            chosen_id = matched.get(part.temp_id)
            is_new = chosen_id is None
            real_id = TreeNoteDigest.new_uuid7() if chosen_id is None else chosen_id
            state.reconciled.append(
                cls.ReconciledPart(
                    part_id=real_id,
                    parent_part_id=parent_real_id,
                    is_new=is_new,
                    position=part.position,
                    part_kind=part.part_kind,
                    heading_level=part.heading_level,
                    title=part.title,
                    content_markdown=part.content_markdown,
                    list_style=part.list_style,
                    language=part.language,
                    checked=part.checked,
                )
            )
            cls._reconcile_level(part.temp_id, real_id, state)

    @classmethod
    def _match_sibling_ids(
        cls,
        new_siblings: list[TreeNotePartsReconciler.ParsedPart],
        old_siblings: list[TreeNotePart],
        used_old_ids: set[str],
    ) -> dict[str, str]:
        """Match sibling parts by exact signature first, then same part_kind ordinal.

        Args:
            new_siblings: Newly parsed sibling blocks under the same container.
            old_siblings: Existing stored sibling ``TreeNotePart`` rows.
            used_old_ids: Running set of already claimed persistent part IDs.

        Returns:
            Mapping from parsed ``temp_id`` to preserved persistent ``part_id``.
        """
        assigned: dict[str, str] = {}
        available = [old for old in old_siblings if old.id not in used_old_ids]
        for part in new_siblings:
            sig = cls._parsed_signature(part)
            match = next(
                (old for old in available if old.id not in used_old_ids and cls._old_signature(old) == sig),
                None,
            )
            if match is not None:
                assigned[part.temp_id] = match.id
                used_old_ids.add(match.id)
        for part in new_siblings:
            if part.temp_id in assigned:
                continue
            match = next(
                (old for old in available if old.id not in used_old_ids and old.part_kind == part.part_kind),
                None,
            )
            if match is not None:
                assigned[part.temp_id] = match.id
                used_old_ids.add(match.id)
        return assigned

    @staticmethod
    def _parsed_signature(
        part: TreeNotePartsReconciler.ParsedPart,
    ) -> tuple[str, int | None, str | None, str, str | None, str | None, bool | None]:
        """Return immutable content signature for exact UUID matching.

        Args:
            part: Newly parsed block descriptor.

        Returns:
            Comparable content tuple identifying the block.
        """
        return (
            part.part_kind,
            part.heading_level,
            part.title,
            part.content_markdown,
            part.list_style,
            part.language,
            part.checked,
        )

    @staticmethod
    def _old_signature(
        part: TreeNotePart,
    ) -> tuple[str, int | None, str | None, str, str | None, str | None, bool | None]:
        """Return immutable content signature from a stored ``TreeNotePart`` row.

        Args:
            part: Existing ORM ``TreeNotePart`` row.

        Returns:
            Normalized content tuple matching ``_parsed_signature``.
        """
        return (
            part.part_kind,
            part.heading_level,
            TreeNoteDigest.normalize_text(part.title) if part.title is not None else None,
            TreeNoteDigest.normalize_text(part.content_markdown),
            part.list_style,
            TreeNoteDigest.normalize_text(part.language) if part.language is not None else None,
            TreeNoteDigest.coerce_checked(part.checked),
        )

    @staticmethod
    def _bottom_up_deleted_ids(existing_parts: list[TreeNotePart], used_old_ids: set[str]) -> list[str]:
        """Return unused old part IDs ordered deepest-child-first for safe deletion.

        Args:
            existing_parts: Existing ORM ``TreeNotePart`` rows of the node.
            used_old_ids: Persistent IDs preserved across reconciliation.

        Returns:
            Unused part UUIDs ordered deepest nested child first.
        """
        by_id = {part.id: part for part in existing_parts}
        depths: dict[str, int] = {}

        def depth(part_id: str) -> int:
            """Compute part nesting depth recursively.

            Args:
                part_id: Target part UUID.

            Returns:
                Non-negative depth from container root.
            """
            if part_id in depths:
                return depths[part_id]
            part = by_id.get(part_id)
            if part is None or part.parent_part_id is None or part.parent_part_id not in by_id:
                depths[part_id] = 0
                return 0
            val = depth(part.parent_part_id) + 1
            depths[part_id] = val
            return val

        unused = [part.id for part in existing_parts if part.id not in used_old_ids]
        unused.sort(key=depth, reverse=True)
        return unused
