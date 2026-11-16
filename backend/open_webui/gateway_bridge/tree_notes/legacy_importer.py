"""Idempotent per-user legacy note importer with strict closed Pydantic probes."""

from __future__ import annotations

import logging
import time

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.gateway_bridge.tree_notes.contracts_parts import TreeNotePartContracts
from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest
from open_webui.gateway_bridge.tree_notes.external_ref_model import TreeNoteExternalRef
from open_webui.gateway_bridge.tree_notes.html_markdown import TreeNoteHtmlConverter
from open_webui.gateway_bridge.tree_notes.node_model import TreeNoteNode
from open_webui.gateway_bridge.tree_notes.part_model import TreeNotePart
from open_webui.gateway_bridge.tree_notes.parts_codec import TreeNotePartsCodec
from open_webui.gateway_bridge.tree_notes.tag_model import TreeNoteTag
from open_webui.models.notes import Note, normalize_pages, sanitize_note_data

log = logging.getLogger(__name__)


def _default_legacy_content() -> TreeNoteLegacyImporter.LegacyContentProbe:
    """Construct an empty ``LegacyContentProbe`` default instance.

    Returns:
        Empty ``LegacyContentProbe`` payload.
    """
    return TreeNoteLegacyImporter.LegacyContentProbe()


class TreeNoteLegacyImporter:
    """Imports user-owned legacy Note records additively into tree_note_* tables."""

    class LegacyTipTapAttrProbe(BaseModel):
        """Closed probe for TipTap node attributes."""

        model_config = ConfigDict(extra='ignore')

        level: int = 1
        language: str = ''
        checked: bool = False
        href: str = ''
        src: str = ''
        alt: str = ''
        id: str = ''
        name: str = ''

    class LegacyTipTapMarkProbe(BaseModel):
        """Closed probe for TipTap inline text marks."""

        model_config = ConfigDict(extra='ignore')

        type: str = ''
        attrs: TreeNoteLegacyImporter.LegacyTipTapAttrProbe | None = None

    class LegacyTipTapNodeProbe(BaseModel):
        """Recursive closed probe for TipTap JSON document nodes."""

        model_config = ConfigDict(extra='ignore')

        type: str = ''
        text: str = ''
        attrs: TreeNoteLegacyImporter.LegacyTipTapAttrProbe | None = None
        marks: list[TreeNoteLegacyImporter.LegacyTipTapMarkProbe] = Field(default_factory=list)
        content: list[TreeNoteLegacyImporter.LegacyTipTapNodeProbe] = Field(default_factory=list)

    class LegacyContentProbe(BaseModel):
        """Closed probe for legacy note/page content envelopes."""

        model_config = ConfigDict(extra='ignore')

        md: str = ''
        html: str = ''
        json_doc: TreeNoteLegacyImporter.LegacyTipTapNodeProbe | None = Field(default=None, alias='json')

    class LegacyPageProbe(BaseModel):
        """Closed probe for normalized legacy note pages."""

        model_config = ConfigDict(extra='ignore')

        id: str = 'main'
        parent_id: str | None = None
        title: str = 'Untitled'
        content: TreeNoteLegacyImporter.LegacyContentProbe = Field(default_factory=_default_legacy_content)

    class LegacyNoteDataProbe(BaseModel):
        """Closed probe for normalized legacy Note.data payloads."""

        model_config = ConfigDict(extra='ignore')

        pages: list[TreeNoteLegacyImporter.LegacyPageProbe] = Field(default_factory=list)

    class LegacyNoteMetaProbe(BaseModel):
        """Closed probe for legacy Note.meta payloads."""

        model_config = ConfigDict(extra='ignore')

        tags: list[str] = Field(default_factory=list)

    class PageImportCandidate(BaseModel):
        """Normalized single page ready for tree_note_node persistence."""

        model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

        page_id: str
        parent_page_id: str | None
        external_id: str
        title: str
        markdown: str

    class ImportContext(BaseModel):
        """Mutable serialized import session context."""

        model_config = ConfigDict(strict=True, arbitrary_types_allowed=True, extra='forbid')

        session: AsyncSession
        owner_user_id: str
        existing_map: dict[str, str]
        next_root_sort: int
        dry_run: bool

    @classmethod
    async def import_for_user(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        request: TreeNotePartContracts.ImportLegacyRequest,
    ) -> tuple[TreeNotePartContracts.ImportLegacyResponse, list[str]]:
        """Import owned legacy notes idempotently inside the caller's serialized transaction.

        Args:
            session: Active serialized write ``AsyncSession``.
            owner_user_id: Verified caller user ID.
            request: Import parameters (optional ``note_ids`` filter and ``dry_run``).

        Returns:
            Tuple of ``(ImportLegacyResponse, missing_requested_note_ids)``.
        """
        notes, missing = await cls._load_target_notes(session, owner_user_id, request.note_ids)
        if missing:
            empty = TreeNotePartContracts.ImportLegacyResponse(
                dry_run=request.dry_run,
                imported_count=0,
                skipped_count=0,
                failed_count=0,
                items=[],
            )
            return empty, missing

        ctx = await cls._build_import_context(session, owner_user_id, request.dry_run)
        items = [await cls._process_legacy_note(ctx, raw_note) for raw_note in notes]
        imported, skipped, failed = cls._tally_import_counts(items)
        response = TreeNotePartContracts.ImportLegacyResponse(
            dry_run=request.dry_run,
            imported_count=imported,
            skipped_count=skipped,
            failed_count=failed,
            items=items,
        )
        return response, []

    @staticmethod
    async def _load_target_notes(
        session: AsyncSession,
        owner_user_id: str,
        note_ids: list[str] | None,
    ) -> tuple[list[Note], list[str]]:
        """Load owner legacy notes optionally filtered by explicit note IDs.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            note_ids: Optional list of requested legacy note IDs.

        Returns:
            Tuple of ``(matched_notes, missing_note_ids)``.
        """
        query = select(Note).where(Note.user_id == owner_user_id).order_by(Note.created_at.asc(), Note.id.asc())
        if note_ids is not None:
            query = query.where(Note.id.in_(note_ids))
        notes = list((await session.execute(query)).scalars().all())
        if note_ids is None:
            return notes, []
        found_ids = {row.id for row in notes}
        return notes, [nid for nid in note_ids if nid not in found_ids]

    @classmethod
    async def _build_import_context(
        cls,
        session: AsyncSession,
        owner_user_id: str,
        dry_run: bool,
    ) -> TreeNoteLegacyImporter.ImportContext:
        """Assemble the owner's idempotent import state prior to note iteration.

        Args:
            session: Active serialized ``AsyncSession``.
            owner_user_id: Verified owner user ID.
            dry_run: Whether to skip persistence mutations.

        Returns:
            Populated ``ImportContext``.
        """
        existing_map = await cls._load_existing_import_map(session, owner_user_id)
        max_sort_stmt = select(func.coalesce(func.max(TreeNoteNode.sort_order), -1)).where(
            TreeNoteNode.owner_user_id == owner_user_id,
            TreeNoteNode.parent_id.is_(None),
        )
        next_root_sort = int((await session.execute(max_sort_stmt)).scalar_one()) + 1
        return cls.ImportContext(
            session=session,
            owner_user_id=owner_user_id,
            existing_map=existing_map,
            next_root_sort=next_root_sort,
            dry_run=dry_run,
        )

    @staticmethod
    def _tally_import_counts(items: list[TreeNotePartContracts.ImportLegacyItemResult]) -> tuple[int, int, int]:
        """Count imported, skipped, and failed legacy notes cleanly.

        Args:
            items: Per-note processing results.

        Returns:
            Tuple of ``(imported_count, skipped_count, failed_count)``.
        """
        imported = sum(1 for item in items if item.status in ('imported', 'would_import'))
        skipped = sum(1 for item in items if item.status == 'skipped_existing')
        failed = sum(1 for item in items if item.status == 'failed')
        return imported, skipped, failed

    @staticmethod
    async def _load_existing_import_map(session: AsyncSession, owner_user_id: str) -> dict[str, str]:
        """Load already-imported external_id -> node_id anchors for one owner.

        Args:
            session: Active ``AsyncSession``.
            owner_user_id: Verified owner user ID.

        Returns:
            Mapping from legacy ``external_id`` to existing ``TreeNoteNode.id``.
        """
        stmt = select(TreeNoteExternalRef.external_id, TreeNoteExternalRef.node_id).where(
            TreeNoteExternalRef.owner_user_id == owner_user_id,
            TreeNoteExternalRef.target_kind == 'imported_note',
        )
        rows = (await session.execute(stmt)).all()
        return {str(ext_id): str(node_id) for ext_id, node_id in rows}

    @classmethod
    async def _process_legacy_note(
        cls,
        ctx: TreeNoteLegacyImporter.ImportContext,
        raw_note: Note,
    ) -> TreeNotePartContracts.ImportLegacyItemResult:
        """Extract pages from one legacy note and persist inside a per-note savepoint.

        Args:
            ctx: Mutable per-owner import context.
            raw_note: Source legacy ``Note`` ORM row.

        Returns:
            Per-note ``ImportLegacyItemResult``.
        """
        safe_title = cls._safe_title(raw_note.title)
        try:
            candidates, tags = cls._extract_note_candidates(raw_note)
        except Exception as exc:
            log.warning('Legacy note %s normalization failed: %s', raw_note.id, type(exc).__name__)
            return TreeNotePartContracts.ImportLegacyItemResult(
                legacy_note_id=raw_note.id,
                title=safe_title,
                status='failed',
                error=type(exc).__name__,
            )

        if all(cand.external_id in ctx.existing_map for cand in candidates):
            return TreeNotePartContracts.ImportLegacyItemResult(
                legacy_note_id=raw_note.id,
                title=safe_title,
                status='skipped_existing',
                node_id=ctx.existing_map.get(raw_note.id),
                page_count=len(candidates),
            )

        if ctx.dry_run:
            return TreeNotePartContracts.ImportLegacyItemResult(
                legacy_note_id=raw_note.id,
                title=safe_title,
                status='would_import',
                node_id=ctx.existing_map.get(raw_note.id),
                page_count=len(candidates),
            )

        try:
            async with ctx.session.begin_nested():
                root_id = await cls._persist_candidates(ctx, raw_note, candidates, tags)
            return TreeNotePartContracts.ImportLegacyItemResult(
                legacy_note_id=raw_note.id,
                title=safe_title,
                status='imported',
                node_id=root_id,
                page_count=len(candidates),
            )
        except Exception as exc:
            log.warning('Legacy note %s persistence failed: %s', raw_note.id, type(exc).__name__)
            return TreeNotePartContracts.ImportLegacyItemResult(
                legacy_note_id=raw_note.id,
                title=safe_title,
                status='failed',
                error=type(exc).__name__,
            )

    @classmethod
    def _extract_note_candidates(
        cls,
        raw_note: Note,
    ) -> tuple[list[TreeNoteLegacyImporter.PageImportCandidate], list[str]]:
        """Normalize legacy note data/meta through upstream helpers and closed probes.

        Args:
            raw_note: Source legacy ``Note`` ORM row.

        Returns:
            Tuple of ``(page_candidates, deduplicated_tag_slugs)``.
        """
        sanitized = sanitize_note_data(raw_note.data)
        normalized = normalize_pages(sanitized, note_title=raw_note.title)
        data_probe = cls.LegacyNoteDataProbe.model_validate(normalized)
        meta_probe = (
            cls.LegacyNoteMetaProbe.model_validate(raw_note.meta)
            if raw_note.meta is not None
            else cls.LegacyNoteMetaProbe()
        )
        tags: list[str] = []
        seen_tags: set[str] = set()
        for raw_tag in meta_probe.tags:
            slug = TreeNoteDigest.coerce_legacy_tag_slug(raw_tag)
            if slug is not None and slug not in seen_tags:
                seen_tags.add(slug)
                tags.append(slug)

        candidates: list[TreeNoteLegacyImporter.PageImportCandidate] = []
        for page in data_probe.pages:
            ext_id = raw_note.id if page.id == 'main' else f'{raw_note.id}:{page.id}'
            title = cls._safe_title(raw_note.title if page.id == 'main' and not page.title.strip() else page.title)
            markdown = cls._extract_page_markdown(page.content)
            candidates.append(
                cls.PageImportCandidate(
                    page_id=page.id,
                    parent_page_id=None if page.id == 'main' else page.parent_id,
                    external_id=ext_id,
                    title=title,
                    markdown=markdown,
                )
            )
        return candidates, tags

    @classmethod
    def _extract_page_markdown(cls, content: TreeNoteLegacyImporter.LegacyContentProbe) -> str:
        """Resolve canonical Markdown via md -> html -> TipTap JSON fallback.

        Args:
            content: Closed probe containing Markdown, HTML, and/or TipTap document AST.

        Returns:
            Normalized canonical Markdown string.
        """
        md = TreeNoteDigest.normalize_text(content.md).strip()
        if md:
            return md
        html = content.html.strip()
        if html:
            converted_html = TreeNoteHtmlConverter.html_to_markdown(html).strip()
            if converted_html:
                return converted_html
        if content.json_doc is not None:
            return cls._tiptap_to_markdown(content.json_doc).strip()
        return ''

    @classmethod
    async def _persist_candidates(
        cls,
        ctx: TreeNoteLegacyImporter.ImportContext,
        raw_note: Note,
        candidates: list[TreeNoteLegacyImporter.PageImportCandidate],
        tags: list[str],
    ) -> str | None:
        """Persist non-existing pages for one legacy note inside its savepoint.

        Args:
            ctx: Mutable per-owner import context.
            raw_note: Source legacy ``Note`` ORM row.
            candidates: Ordered normalized page candidates.
            tags: Validated legacy note tag slugs.

        Returns:
            Root ``main`` node ID for the imported legacy note, if any.
        """
        now_ns = time.time_ns()
        created_at = raw_note.created_at if (raw_note.created_at is not None and raw_note.created_at >= 0) else now_ns
        updated_raw = raw_note.updated_at if raw_note.updated_at is not None else created_at
        timestamps = (created_at, max(updated_raw, created_at), now_ns)
        child_sort_counts: dict[str, int] = {}
        page_to_node = {cand.page_id: ctx.existing_map[cand.external_id] for cand in candidates if cand.external_id in ctx.existing_map}

        for cand in candidates:
            if cand.external_id in ctx.existing_map:
                continue
            parent_node_id, sort_order = await cls._resolve_parent_and_sort(ctx, cand, page_to_node, child_sort_counts)
            new_id = await cls._insert_single_candidate(ctx, cand, (parent_node_id, sort_order), tags, timestamps)
            ctx.existing_map[cand.external_id] = new_id
            page_to_node[cand.page_id] = new_id

        return page_to_node.get('main')

    @classmethod
    async def _insert_single_candidate(
        cls,
        ctx: TreeNoteLegacyImporter.ImportContext,
        cand: TreeNoteLegacyImporter.PageImportCandidate,
        placement: tuple[str | None, int],
        tags: list[str],
        timestamps: tuple[int, int, int],
    ) -> str:
        """Insert a single normalized page candidate with parts, tags, and import anchor.

        Args:
            ctx: Mutable per-owner import context.
            cand: Normalized page candidate.
            placement: Tuple of ``(parent_node_id, sort_order)``.
            tags: Normalized tag slugs attached to the root ``main`` page.
            timestamps: Tuple of ``(created_at, updated_at, now_ns)``.

        Returns:
            Newly persisted ``TreeNoteNode.id``.
        """
        parent_node_id, sort_order = placement
        created_at, updated_at, now_ns = timestamps
        new_node_id = TreeNoteDigest.new_uuid7()
        ctx.session.add(
            TreeNoteNode(
                id=new_node_id,
                owner_user_id=ctx.owner_user_id,
                parent_id=parent_node_id,
                sort_order=sort_order,
                title=cand.title,
                node_kind='note',
                archived_at=None,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        await ctx.session.flush()
        cls._add_parsed_parts(ctx.session, new_node_id, cand.markdown, updated_at)
        if cand.page_id == 'main':
            for slug in tags:
                ctx.session.add(TreeNoteTag(node_id=new_node_id, tag_slug=slug))
        ctx.session.add(
            TreeNoteExternalRef(
                id=TreeNoteDigest.new_uuid7(),
                node_id=new_node_id,
                owner_user_id=ctx.owner_user_id,
                target_kind='imported_note',
                external_id=cand.external_id,
                created_at=now_ns,
            )
        )
        await ctx.session.flush()
        return new_node_id

    @classmethod
    async def _resolve_parent_and_sort(
        cls,
        ctx: TreeNoteLegacyImporter.ImportContext,
        cand: TreeNoteLegacyImporter.PageImportCandidate,
        page_to_node: dict[str, str],
        child_sort_counts: dict[str, int],
    ) -> tuple[str | None, int]:
        """Resolve parent node with partial re-import fallback and allocate sibling sort_order.

        Args:
            ctx: Mutable per-owner import context.
            cand: Normalized page candidate.
            page_to_node: Map of ``page_id -> node_id`` within this note import.
            child_sort_counts: Cached per-parent next ``sort_order`` counters.

        Returns:
            Tuple of ``(resolved_parent_node_id, sort_order)``.
        """
        parent_node_id: str | None = None
        if cand.page_id != 'main':
            if cand.parent_page_id and cand.parent_page_id in page_to_node:
                parent_node_id = page_to_node[cand.parent_page_id]
            elif 'main' in page_to_node:
                parent_node_id = page_to_node['main']
        if parent_node_id is not None:
            still_exists = (
                await ctx.session.execute(
                    select(TreeNoteNode.id).where(
                        TreeNoteNode.id == parent_node_id,
                        TreeNoteNode.owner_user_id == ctx.owner_user_id,
                    )
                )
            ).scalar_one_or_none()
            if still_exists is None:
                parent_node_id = None

        if parent_node_id is None:
            sort_order = ctx.next_root_sort
            ctx.next_root_sort += 1
            return None, sort_order

        if parent_node_id not in child_sort_counts:
            stmt = select(func.coalesce(func.max(TreeNoteNode.sort_order), -1)).where(
                TreeNoteNode.owner_user_id == ctx.owner_user_id,
                TreeNoteNode.parent_id == parent_node_id,
            )
            child_sort_counts[parent_node_id] = int((await ctx.session.execute(stmt)).scalar_one()) + 1
        sort_order = child_sort_counts[parent_node_id]
        child_sort_counts[parent_node_id] = sort_order + 1
        return parent_node_id, sort_order

    @staticmethod
    def _add_parsed_parts(session: AsyncSession, node_id: str, markdown: str, updated_at: int) -> None:
        """Parse Markdown blocks into ``TreeNotePart`` rows attached to ``node_id``.

        Args:
            session: Active ``AsyncSession``.
            node_id: Target parent ``TreeNoteNode.id``.
            markdown: Normalized Markdown content.
            updated_at: Nanosecond timestamp applied to inserted parts.
        """
        parsed = TreeNotePartsCodec.parse_markdown(markdown)
        temp_to_id: dict[str, str] = {}
        for part in parsed:
            part_id = TreeNoteDigest.new_uuid7()
            temp_to_id[part.temp_id] = part_id
            parent_id = temp_to_id[part.parent_temp_id] if part.parent_temp_id is not None else None
            session.add(
                TreeNotePart(
                    id=part_id,
                    node_id=node_id,
                    parent_part_id=parent_id,
                    position=part.position,
                    part_kind=part.part_kind,
                    heading_level=part.heading_level,
                    title=part.title,
                    content_markdown=part.content_markdown,
                    list_style=part.list_style,
                    language=part.language,
                    checked=part.checked,
                    updated_at=updated_at,
                )
            )

    @staticmethod
    def _safe_title(raw_title: str | None) -> str:
        """Sanitize nullable legacy note titles to a valid single-line NFC title.

        Args:
            raw_title: Optional legacy note or page title string.

        Returns:
            Non-empty trimmed single-line title bounded to 255 characters.
        """
        text = TreeNoteDigest.normalize_text(raw_title or '')
        cleaned = ''.join(' ' if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text).strip()
        if not cleaned:
            return 'Untitled'
        return cleaned[:255].strip() or 'Untitled'

    @classmethod
    def _tiptap_to_markdown(cls, node: TreeNoteLegacyImporter.LegacyTipTapNodeProbe) -> str:
        """Convert a validated TipTap AST root node into canonical Markdown.

        Args:
            node: Parsed TipTap root AST node probe.

        Returns:
            Rendered Markdown string.
        """
        if node.type == 'doc':
            blocks = [cls._tiptap_block(child) for child in node.content]
            return '\n\n'.join(chunk for chunk in blocks if chunk.strip())
        return cls._tiptap_block(node)

    @classmethod
    def _tiptap_block(cls, node: TreeNoteLegacyImporter.LegacyTipTapNodeProbe) -> str:
        """Convert one TipTap block node recursively to Markdown.

        Args:
            node: TipTap block AST node.

        Returns:
            Rendered Markdown block string.
        """
        if node.type == 'heading':
            level = min(6, max(1, node.attrs.level if node.attrs is not None else 1))
            return f'{"#" * level} {cls._tiptap_inline(node).strip()}'
        if node.type == 'paragraph':
            return cls._tiptap_inline(node).strip()
        if node.type == 'horizontalRule':
            return '---'
        if node.type == 'codeBlock':
            lang = node.attrs.language.strip()[:64] if node.attrs is not None else ''
            body = ''.join(child.text for child in node.content)
            return f'```{lang}\n{body}\n```'
        if node.type == 'blockquote':
            inner = '\n\n'.join(cls._tiptap_block(child) for child in node.content).strip()
            return '\n'.join(f'> {line}' if line else '>' for line in inner.split('\n'))
        if node.type in {'bulletList', 'orderedList', 'taskList'}:
            return cls._tiptap_list(node)
        if node.type == 'fileAttachment' and node.attrs is not None and node.attrs.id:
            label = (node.attrs.name or node.attrs.id).replace('[', '').replace(']', '')
            return f'[{label}](/api/v1/files/{node.attrs.id}/content)'
        if node.type == 'image' and node.attrs is not None and node.attrs.src:
            return f'![{node.attrs.alt}]({node.attrs.src})'
        return '\n\n'.join(cls._tiptap_block(child) for child in node.content)

    @classmethod
    def _tiptap_list(cls, node: TreeNoteLegacyImporter.LegacyTipTapNodeProbe) -> str:
        """Convert a TipTap bullet, ordered, or task list to Markdown.

        Args:
            node: TipTap list AST node.

        Returns:
            Markdown formatted list string.
        """
        ordered = node.type == 'orderedList'
        lines: list[str] = []
        for idx, item in enumerate(node.content):
            prefix = f'{idx + 1}. ' if ordered else '- '
            checkbox = ''
            if item.type == 'taskItem':
                checked = item.attrs.checked if item.attrs is not None else False
                checkbox = '[x] ' if checked else '[ ] '
            parts = [cls._tiptap_block(child) for child in item.content]
            non_empty = [p for p in parts if p.strip()]
            first = non_empty[0] if non_empty else ''
            lines.append(f'{prefix}{checkbox}{first}'.rstrip())
            indent = ' ' * len(prefix)
            for extra in non_empty[1:]:
                for sub_line in extra.split('\n'):
                    lines.append(f'{indent}{sub_line}' if sub_line else '')
        return '\n'.join(lines)

    @classmethod
    def _tiptap_inline(cls, node: TreeNoteLegacyImporter.LegacyTipTapNodeProbe) -> str:
        """Convert inline TipTap text and marks to Markdown inline syntax.

        Args:
            node: TipTap inline AST node.

        Returns:
            Inline Markdown text.
        """
        if node.type == 'hardBreak':
            return '\n'
        if node.type == 'text':
            return cls._apply_tiptap_marks(node.text, node.marks)
        if node.type == 'fileAttachment' and node.attrs is not None and node.attrs.id:
            label = (node.attrs.name or node.attrs.id).replace('[', '').replace(']', '')
            return f'[{label}](/api/v1/files/{node.attrs.id}/content)'
        if node.type == 'image' and node.attrs is not None and node.attrs.src:
            return f'![{node.attrs.alt}]({node.attrs.src})'
        return ''.join(cls._tiptap_inline(child) for child in node.content)

    @staticmethod
    def _apply_tiptap_marks(
        text: str,
        marks: list[TreeNoteLegacyImporter.LegacyTipTapMarkProbe],
    ) -> str:
        """Wrap TipTap inline text in Markdown mark delimiters.

        Args:
            text: Leaf text content.
            marks: Ordered TipTap inline formatting marks.

        Returns:
            Markdown-wrapped inline text.
        """
        result = text
        for mark in marks:
            if mark.type == 'bold':
                result = f'**{result}**'
            elif mark.type == 'italic':
                result = f'*{result}*'
            elif mark.type == 'code':
                result = f'`{result}`'
            elif mark.type == 'strike':
                result = f'~~{result}~~'
            elif mark.type == 'link' and mark.attrs is not None and mark.attrs.href:
                result = f'[{result}]({mark.attrs.href})'
        return result

    @classmethod
    def rebuild_models(cls) -> None:
        """Rebuild nested Pydantic probe models to resolve recursive forward references."""
        for model_cls in (
            cls.LegacyTipTapNodeProbe,
            cls.LegacyContentProbe,
            cls.LegacyPageProbe,
            cls.LegacyNoteDataProbe,
        ):
            model_cls.model_rebuild()


TreeNoteLegacyImporter.rebuild_models()
