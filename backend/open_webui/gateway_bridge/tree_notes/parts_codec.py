"""Lossless Markdown <-> tree_note_part codec, dotted addressing, and UUID reconciliation."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_reconciler import (
    ListStyle,
    PartKind,
    TreeNotePartsReconciler,
)

HEADING_RE = re.compile(r'^\s{0,3}(#{1,6})(?:\s+(.*?))?\s*$')
RULE_RE = re.compile(r'^\s{0,3}([-*_])(?:\s*\1){2,}\s*$')
FENCE_OPEN_RE = re.compile(r'^\s{0,3}(`{3,}|~{3,})(.*)$')
TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$')
UL_ITEM_RE = re.compile(r'^(\s*)([-*+])\s+(.*)$')
OL_ITEM_RE = re.compile(r'^(\s*)(\d{1,9})([.)])\s+(.*)$')
TASK_PREFIX_RE = re.compile(r'^\[([ xX])\]\s*(.*)$')
QUOTE_RE = re.compile(r'^\s{0,3}> ?(.*)$')
FILE_LINK_RE = re.compile(r'/api/v1/files/([\w-]+)/content')

ALLOWED_CHILDREN: dict[str | None, frozenset[str]] = {
    None: frozenset({'section', 'paragraph', 'list', 'fence', 'table', 'quote', 'rule'}),
    'section': frozenset({'section', 'paragraph', 'list', 'fence', 'table', 'quote', 'rule'}),
    'paragraph': frozenset(),
    'list': frozenset({'item'}),
    'item': frozenset({'list', 'paragraph', 'fence', 'quote'}),
    'fence': frozenset(),
    'table': frozenset(),
    'quote': frozenset({'paragraph', 'list', 'fence', 'table', 'quote', 'rule'}),
    'rule': frozenset(),
}


class TreeNotePartsCodec:
    """Lossless Markdown AST parser, renderer, HTML/TipTap converter, and UUID reconciler."""

    MAX_SECTION_DEPTH = 6
    ParsedPart = TreeNotePartsReconciler.ParsedPart
    ReconciledPart = TreeNotePartsReconciler.ReconciledPart

    class _ScanState(BaseModel):
        """Mutable parser state passed cleanly across block scanners."""

        model_config = ConfigDict(strict=True, extra='forbid')

        lines: list[str]
        index: int = 0
        seq: int = 0
        section_stack: list[tuple[int, str]] = []
        sibling_counts: dict[str | None, int] = {}
        parts: list[TreeNotePartsReconciler.ParsedPart] = []

    class _ListFrame(BaseModel):
        """Active list item specification during nested list parsing."""

        model_config = ConfigDict(strict=True, extra='forbid')

        indent: int
        style: ListStyle
        lead: str
        checked: bool | None

    @classmethod
    def validate_parent_child_kind(
        cls,
        parent_kind: str | None,
        child_kind: str,
        parent_level: int | None,
        child_level: int | None,
    ) -> None:
        """Validate container/child part nesting and section depth rules.

        Args:
            parent_kind: Container ``part_kind``, or ``None`` for top-level parts.
            child_kind: Candidate child ``part_kind``.
            parent_level: Heading level of parent when ``parent_kind == 'section'``.
            child_level: Heading level of child when ``child_kind == 'section'``.

        Raises:
            ValueError: Nesting violates the closed part matrix or heading-level order.
        """
        allowed = ALLOWED_CHILDREN[parent_kind] if parent_kind in ALLOWED_CHILDREN else None
        if allowed is None or child_kind not in allowed:
            raise ValueError(f'part_kind {child_kind!r} is not allowed under {parent_kind!r}.')
        if parent_kind == 'section' and child_kind == 'section':
            if parent_level is None or child_level is None or not (parent_level < child_level <= 6):
                raise ValueError('Nested section heading_level must exceed parent heading_level (<= 6).')

    @classmethod
    def extract_file_ids(cls, markdown: str) -> set[str]:
        """Extract all referenced ``/api/v1/files/{id}/content`` identifiers from Markdown.

        Args:
            markdown: Normalized Markdown text.

        Returns:
            Set of referenced Open WebUI file IDs.
        """
        return set(FILE_LINK_RE.findall(markdown))

    @classmethod
    def parse_markdown(cls, markdown: str) -> list[TreeNotePartsCodec.ParsedPart]:
        """Parse normalized Markdown into an ordered tree of ``ParsedPart`` blocks.

        Args:
            markdown: Normalized Markdown document string.

        Returns:
            Top-down ordered list of validated ``ParsedPart`` blocks.
        """
        normalized = TreeNoteDigest.normalize_text(markdown)
        if not normalized.strip():
            return []
        state = cls._ScanState(lines=normalized.split('\n'))
        cls._scan_stream(state, allow_sections=True, fixed_parent_id=None)
        return state.parts

    @classmethod
    def _scan_stream(
        cls,
        state: TreeNotePartsCodec._ScanState,
        allow_sections: bool,
        fixed_parent_id: str | None,
    ) -> None:
        """Scan lines until EOF and emit blocks under the current section or fixed parent.

        Args:
            state: Mutable block scanner cursor and accumulators.
            allow_sections: Whether ATX headings should open nested ``section`` parts.
            fixed_parent_id: Constant container temp ID when ``allow_sections`` is False.
        """
        total = len(state.lines)
        while state.index < total:
            line = state.lines[state.index]
            if not line.strip():
                state.index += 1
                continue
            parent_id = cls._current_parent(state, allow_sections, fixed_parent_id)
            if allow_sections and cls._try_heading(state, line):
                continue
            if cls._try_fence(state, line, parent_id):
                continue
            if cls._try_rule(state, line, parent_id):
                continue
            if cls._try_quote(state, line, parent_id):
                continue
            if cls._try_table(state, line, parent_id):
                continue
            if cls._try_list(state, line, parent_id):
                continue
            cls._consume_paragraph(state, parent_id, allow_sections)

    @staticmethod
    def _current_parent(
        state: TreeNotePartsCodec._ScanState,
        allow_sections: bool,
        fixed_parent_id: str | None,
    ) -> str | None:
        """Resolve the active parent temporary ID for the next parsed block.

        Args:
            state: Current scanner cursor and section stack.
            allow_sections: Whether hierarchical headings own top-level blocks.
            fixed_parent_id: Constant fallback container ID.

        Returns:
            Temporary parent part identifier or ``None`` for document root.
        """
        if not allow_sections:
            return fixed_parent_id
        return state.section_stack[-1][1] if state.section_stack else None

    @classmethod
    def _emit_part(
        cls,
        state: TreeNotePartsCodec._ScanState,
        parent_id: str | None,
        part_kind: PartKind,
        spec: TreeNotePartsReconciler.ParsedPart | None = None,
    ) -> str:
        """Append a ``ParsedPart`` with deterministic sibling position and return its temp ID.

        Args:
            state: Mutable scanner state.
            parent_id: Active temporary parent ID or ``None``.
            part_kind: Block kind identifier when ``spec`` is omitted.
            spec: Optional populated ``ParsedPart`` template.

        Returns:
            Newly assigned temporary block identifier.
        """
        state.seq += 1
        pos = state.sibling_counts[parent_id] if parent_id in state.sibling_counts else 0
        state.sibling_counts[parent_id] = pos + 1
        temp_id = f'p_{state.seq}'
        if spec is None:
            part = cls.ParsedPart(temp_id=temp_id, parent_temp_id=parent_id, position=pos, part_kind=part_kind)
        else:
            part = spec.model_copy(update={'temp_id': temp_id, 'parent_temp_id': parent_id, 'position': pos})
        state.parts.append(part)
        return temp_id

    @classmethod
    def _try_heading(cls, state: TreeNotePartsCodec._ScanState, line: str) -> bool:
        """Parse an ATX heading line into a ``section`` and update the section stack.

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.

        Returns:
            True if the line matched a heading and advanced ``state.index``.
        """
        match = HEADING_RE.fullmatch(line)
        if match is None:
            return False
        hashes, raw_title = match.groups()
        level = len(hashes)
        cleaned = (raw_title or '').strip()
        cleaned = re.sub(r'\s+#+\s*$', '', cleaned).strip()
        if not cleaned:
            return False
        title = TreeNoteDigest.normalize_single_line(cleaned[:1000], 'title', 1000)
        while state.section_stack and state.section_stack[-1][0] >= level:
            state.section_stack.pop()
        parent_id = state.section_stack[-1][1] if state.section_stack else None
        spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=parent_id,
            position=0,
            part_kind='section',
            heading_level=level,
            title=title,
        )
        section_id = cls._emit_part(state, parent_id, 'section', spec)
        state.section_stack.append((level, section_id))
        state.index += 1
        return True

    @classmethod
    def _try_fence(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        parent_id: str | None,
    ) -> bool:
        """Parse a fenced code block starting at ``state.index``.

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.
            parent_id: Active container temp ID or ``None``.

        Returns:
            True if a fenced block was parsed.
        """
        match = FENCE_OPEN_RE.fullmatch(line)
        if match is None:
            return False
        marker, raw_info = match.groups()
        fence_char = marker[0]
        fence_len = len(marker)
        info = TreeNoteDigest.normalize_text(raw_info).strip()
        language = info[:64].strip() if info else None
        if language is not None and ('\n' in language or '\t' in language):
            language = language.split()[0][:64]
        state.index += 1
        body_lines: list[str] = []
        close_re = re.compile(rf'^\s{{0,3}}{re.escape(fence_char)}{{{fence_len},}}\s*$')
        while state.index < len(state.lines):
            candidate = state.lines[state.index]
            state.index += 1
            if close_re.fullmatch(candidate) is not None:
                break
            body_lines.append(candidate)
        spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=parent_id,
            position=0,
            part_kind='fence',
            content_markdown='\n'.join(body_lines),
            language=language if language else None,
        )
        cls._emit_part(state, parent_id, 'fence', spec)
        return True

    @classmethod
    def _try_rule(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        parent_id: str | None,
    ) -> bool:
        """Parse a Markdown thematic break (horizontal rule).

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.
            parent_id: Active container temp ID or ``None``.

        Returns:
            True if the line matched a thematic break.
        """
        if RULE_RE.fullmatch(line) is None:
            return False
        cls._emit_part(state, parent_id, 'rule')
        state.index += 1
        return True

    @classmethod
    def _try_table(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        parent_id: str | None,
    ) -> bool:
        """Parse a GFM pipe table starting at ``state.index``.

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.
            parent_id: Active container temp ID or ``None``.

        Returns:
            True if a valid GFM table block was parsed.
        """
        if '|' not in line or state.index + 1 >= len(state.lines):
            return False
        sep_line = state.lines[state.index + 1]
        if '|' not in sep_line or TABLE_SEP_RE.fullmatch(sep_line) is None:
            return False
        table_lines = [line.rstrip(), sep_line.rstrip()]
        state.index += 2
        while state.index < len(state.lines):
            row = state.lines[state.index]
            if not row.strip() or '|' not in row or HEADING_RE.fullmatch(row) is not None:
                break
            table_lines.append(row.rstrip())
            state.index += 1
        spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=parent_id,
            position=0,
            part_kind='table',
            content_markdown='\n'.join(table_lines),
        )
        cls._emit_part(state, parent_id, 'table', spec)
        return True

    @classmethod
    def _try_quote(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        parent_id: str | None,
    ) -> bool:
        """Parse a blockquote block and emit nested child blocks when structured.

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.
            parent_id: Active container temp ID or ``None``.

        Returns:
            True if a blockquote block was parsed.
        """
        if QUOTE_RE.fullmatch(line) is None:
            return False
        inner_lines: list[str] = []
        while state.index < len(state.lines):
            match = QUOTE_RE.fullmatch(state.lines[state.index])
            if match is None:
                break
            inner_lines.append(match.group(1))
            state.index += 1
        inner_text = '\n'.join(inner_lines).strip('\n')
        if not inner_text.strip():
            cls._emit_part(state, parent_id, 'quote')
            return True
        sub_state = cls._ScanState(lines=inner_text.split('\n'), seq=state.seq)
        cls._scan_stream(sub_state, allow_sections=False, fixed_parent_id='__quote__')
        if len(sub_state.parts) == 1 and sub_state.parts[0].part_kind == 'paragraph':
            spec = cls.ParsedPart(
                temp_id='',
                parent_temp_id=parent_id,
                position=0,
                part_kind='quote',
                content_markdown=sub_state.parts[0].content_markdown,
            )
            cls._emit_part(state, parent_id, 'quote', spec)
            return True
        quote_id = cls._emit_part(state, parent_id, 'quote')
        sub_remap = cls._ScanState(lines=inner_text.split('\n'), seq=state.seq)
        cls._scan_stream(sub_remap, allow_sections=False, fixed_parent_id=quote_id)
        state.seq = sub_remap.seq
        state.parts.extend(sub_remap.parts)
        return True

    @classmethod
    def _match_list_line(cls, line: str) -> _ListFrame | None:
        """Match an unordered or ordered list item line and optional task checkbox.

        Args:
            line: Source Markdown line to test.

        Returns:
            Parsed ``_ListFrame`` when line starts a list item, otherwise ``None``.
        """
        ul_match = UL_ITEM_RE.fullmatch(line)
        if ul_match is not None:
            indent_str, _, raw_body = ul_match.groups()
            checked, lead = cls._split_checkbox(raw_body)
            return cls._ListFrame(indent=len(indent_str.expandtabs(4)), style='unordered', lead=lead, checked=checked)
        ol_match = OL_ITEM_RE.fullmatch(line)
        if ol_match is not None:
            indent_str, _, _, raw_body = ol_match.groups()
            checked, lead = cls._split_checkbox(raw_body)
            return cls._ListFrame(indent=len(indent_str.expandtabs(4)), style='ordered', lead=lead, checked=checked)
        return None

    @staticmethod
    def _split_checkbox(raw_body: str) -> tuple[bool | None, str]:
        """Extract GFM task checkbox state from a list item lead line.

        Args:
            raw_body: Remainder of list item line after marker.

        Returns:
            Tuple of ``(checked_bool_or_none, remaining_text)``.
        """
        task_match = TASK_PREFIX_RE.fullmatch(raw_body.strip())
        if task_match is None:
            return None, raw_body.strip()
        mark, text = task_match.groups()
        return mark.lower() == 'x', text.strip()

    @classmethod
    def _try_list(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        parent_id: str | None,
    ) -> bool:
        """Parse a contiguous Markdown list including nested sublists and child blocks.

        Args:
            state: Mutable scanner state.
            line: Current Markdown source line.
            parent_id: Active container temp ID or ``None``.

        Returns:
            True if a list container was parsed.
        """
        first = cls._match_list_line(line)
        if first is None:
            return False
        cls._consume_list_at_indent(state, parent_id, first.indent, first.style)
        return True

    @classmethod
    def _consume_list_at_indent(
        cls,
        state: TreeNotePartsCodec._ScanState,
        parent_id: str | None,
        base_indent: int,
        style: ListStyle,
    ) -> None:
        """Consume all consecutive items belonging to one list container.

        Args:
            state: Mutable scanner state.
            parent_id: Parent container temporary ID or ``None``.
            base_indent: Column indentation of the list's bullet markers.
            style: List style (``'unordered'`` or ``'ordered'``).
        """
        list_spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=parent_id,
            position=0,
            part_kind='list',
            list_style=style,
        )
        list_id = cls._emit_part(state, parent_id, 'list', list_spec)
        while state.index < len(state.lines):
            candidate = state.lines[state.index]
            if not candidate.strip():
                if not cls._has_list_continuation(state, base_indent, style):
                    break
                state.index += 1
                continue
            frame = cls._match_list_line(candidate)
            if frame is None or frame.indent != base_indent or frame.style != style:
                break
            cls._consume_single_item(state, list_id, base_indent, frame)

    @classmethod
    def _has_list_continuation(
        cls,
        state: TreeNotePartsCodec._ScanState,
        base_indent: int,
        style: ListStyle,
    ) -> bool:
        """Look ahead past blank lines to check if the current list continues.

        Args:
            state: Current scanner cursor.
            base_indent: Indentation column of the active list.
            style: Expected list style.

        Returns:
            True if the next non-blank line continues the same list.
        """
        probe = state.index + 1
        while probe < len(state.lines) and not state.lines[probe].strip():
            probe += 1
        if probe >= len(state.lines):
            return False
        frame = cls._match_list_line(state.lines[probe])
        return frame is not None and frame.indent == base_indent and frame.style == style

    @classmethod
    def _consume_single_item(
        cls,
        state: TreeNotePartsCodec._ScanState,
        list_id: str,
        base_indent: int,
        frame: _ListFrame,
    ) -> None:
        """Emit one list item and parse any indented child blocks beneath it.

        Args:
            state: Mutable scanner state.
            list_id: Parent list temporary identifier.
            base_indent: Indentation column of the owning list.
            frame: Parsed list item lead line metadata.
        """
        item_spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=list_id,
            position=0,
            part_kind='item',
            content_markdown=frame.lead,
            checked=frame.checked,
        )
        item_id = cls._emit_part(state, list_id, 'item', item_spec)
        state.index += 1
        child_lines = cls._collect_indented_lines(state, base_indent)
        if not child_lines:
            return
        sub_state = cls._ScanState(lines=child_lines, seq=state.seq)
        cls._scan_item_children(sub_state, item_id)
        state.seq = sub_state.seq
        state.parts.extend(sub_state.parts)

    @classmethod
    def _collect_indented_lines(cls, state: TreeNotePartsCodec._ScanState, base_indent: int) -> list[str]:
        """Collect indented continuation lines strictly deeper than ``base_indent``.

        Args:
            state: Mutable scanner state.
            base_indent: Owning list indentation column.

        Returns:
            List of dedented child block lines.
        """
        collected: list[str] = []
        min_child_indent = base_indent + 2
        while state.index < len(state.lines):
            raw = state.lines[state.index]
            if not raw.strip():
                next_idx = state.index + 1
                if next_idx < len(state.lines) and cls._line_indent(state.lines[next_idx]) >= min_child_indent:
                    collected.append('')
                    state.index += 1
                    continue
                break
            if cls._line_indent(raw) < min_child_indent:
                break
            collected.append(cls._dedent_line(raw, min_child_indent))
            state.index += 1
        return collected

    @staticmethod
    def _line_indent(line: str) -> int:
        """Count leading visual spaces on a line with 4-space tab stops.

        Args:
            line: Source text line.

        Returns:
            Number of leading visual columns.
        """
        expanded = line.expandtabs(4)
        return len(expanded) - len(expanded.lstrip(' '))

    @staticmethod
    def _dedent_line(line: str, spaces: int) -> str:
        """Strip at most ``spaces`` leading columns after tab expansion.

        Args:
            line: Source text line.
            spaces: Maximum leading spaces to remove.

        Returns:
            Dedent-adjusted string.
        """
        expanded = line.expandtabs(4)
        return expanded[min(spaces, len(expanded) - len(expanded.lstrip(' '))):]

    @classmethod
    def _scan_item_children(cls, sub_state: TreeNotePartsCodec._ScanState, item_id: str) -> None:
        """Parse nested blocks allowed inside a list item (list, paragraph, fence, quote).

        Args:
            sub_state: Sub-scanner state for dedented item child lines.
            item_id: Parent item temporary identifier.
        """
        total = len(sub_state.lines)
        while sub_state.index < total:
            line = sub_state.lines[sub_state.index]
            if not line.strip():
                sub_state.index += 1
                continue
            if cls._try_fence(sub_state, line, item_id):
                continue
            if cls._try_quote_for_item(sub_state, line, item_id):
                continue
            if cls._try_list(sub_state, line, item_id):
                continue
            cls._consume_paragraph(sub_state, item_id, allow_sections=False)

    @classmethod
    def _try_quote_for_item(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        item_id: str,
    ) -> bool:
        """Parse a simple blockquote nested directly inside a list item.

        Args:
            state: Mutable sub-scanner state.
            line: Current source line inside the item.
            item_id: Parent item temporary identifier.

        Returns:
            True if a nested quote block was parsed.
        """
        if QUOTE_RE.fullmatch(line) is None:
            return False
        quoted: list[str] = []
        while state.index < len(state.lines):
            match = QUOTE_RE.fullmatch(state.lines[state.index])
            if match is None:
                break
            quoted.append(match.group(1))
            state.index += 1
        spec = cls.ParsedPart(
            temp_id='',
            parent_temp_id=item_id,
            position=0,
            part_kind='quote',
            content_markdown='\n'.join(quoted).strip(),
        )
        cls._emit_part(state, item_id, 'quote', spec)
        return True

    @classmethod
    def _consume_paragraph(
        cls,
        state: TreeNotePartsCodec._ScanState,
        parent_id: str | None,
        allow_sections: bool,
    ) -> None:
        """Consume contiguous non-empty lines until blank line or block starter.

        Args:
            state: Mutable scanner state.
            parent_id: Active container temporary ID or ``None``.
            allow_sections: Whether ATX headings break the paragraph.
        """
        para_lines: list[str] = []
        while state.index < len(state.lines):
            line = state.lines[state.index]
            if not line.strip():
                break
            if para_lines and cls._starts_new_block(state, line, allow_sections):
                break
            para_lines.append(line.rstrip())
            state.index += 1
        text = '\n'.join(para_lines).strip()
        if text:
            spec = cls.ParsedPart(
                temp_id='',
                parent_temp_id=parent_id,
                position=0,
                part_kind='paragraph',
                content_markdown=text,
            )
            cls._emit_part(state, parent_id, 'paragraph', spec)

    @classmethod
    def _starts_new_block(
        cls,
        state: TreeNotePartsCodec._ScanState,
        line: str,
        allow_sections: bool,
    ) -> bool:
        """Return whether ``line`` begins a non-paragraph Markdown block.

        Args:
            state: Current scanner cursor for pipe-table lookahead.
            line: Candidate Markdown line.
            allow_sections: Whether ATX headings count as block starters.

        Returns:
            True if ``line`` opens a non-paragraph block.
        """
        if allow_sections and HEADING_RE.fullmatch(line) is not None:
            return True
        if FENCE_OPEN_RE.fullmatch(line) is not None or RULE_RE.fullmatch(line) is not None:
            return True
        if QUOTE_RE.fullmatch(line) is not None or cls._match_list_line(line) is not None:
            return True
        if '|' in line and state.index + 1 < len(state.lines):
            if TABLE_SEP_RE.fullmatch(state.lines[state.index + 1]) is not None:
                return True
        return False

    @classmethod
    def render_markdown(cls, parts: list[TreeNotePart]) -> str:
        """Render an ordered ORM ``TreeNotePart`` tree back into canonical Markdown.

        Args:
            parts: All ``TreeNotePart`` rows belonging to one node.

        Returns:
            Canonical Markdown text using LF newlines.
        """
        return TreeNotePartsReconciler.render_markdown(parts)

    @classmethod
    def render_parsed_parts(cls, parts: list[TreeNotePartsReconciler.ParsedPart]) -> str:
        """Render a list of ``ParsedPart`` blocks into canonical Markdown.

        Args:
            parts: Parsed block tree list.

        Returns:
            Canonical Markdown document string.
        """
        return TreeNotePartsReconciler.render_parsed_parts(parts)

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
        return TreeNotePartsReconciler.reconcile_parts(existing_parts, parsed_parts)
