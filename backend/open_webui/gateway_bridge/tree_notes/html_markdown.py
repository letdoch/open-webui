"""HTML-to-Markdown conversion helper for legacy note import fallback."""

from __future__ import annotations

from bs4 import BeautifulSoup, PageElement, Tag

from open_webui.gateway_bridge.tree_notes.digest import TreeNoteDigest


class TreeNoteHtmlConverter:
    """Converts legacy HTML note bodies cleanly to normalized Markdown."""

    @classmethod
    def html_to_markdown(cls, html: str) -> str:
        """Convert legacy HTML note fragments cleanly into canonical Markdown.

        Args:
            html: Raw HTML string from legacy note content.

        Returns:
            Normalized Markdown string.
        """
        if not html.strip():
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        root = soup.body if soup.body is not None else soup
        blocks = [cls._html_block_to_md(child) for child in root.children]
        return TreeNoteDigest.normalize_text('\n\n'.join(chunk for chunk in blocks if chunk.strip())).strip()

    @classmethod
    def _html_block_to_md(cls, node: PageElement) -> str:
        """Convert one BeautifulSoup node into Markdown text.

        Args:
            node: Parsed HTML DOM node or navigable text element.

        Returns:
            Markdown fragment string.
        """
        if not isinstance(node, Tag):
            return str(node).strip()
        name = node.name.lower()
        if name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            level = int(name[1])
            return f'{"#" * level} {cls._html_inline_to_md(node).strip()}'
        if name == 'hr':
            return '---'
        if name == 'pre':
            code = node.find('code')
            text = (code.get_text() if code is not None else node.get_text()).strip('\n')
            return f'```\n{text}\n```'
        if name in {'ul', 'ol'}:
            return cls._html_list_to_md(node, ordered=(name == 'ol'))
        if name == 'blockquote':
            inner = '\n'.join(cls._html_block_to_md(c) for c in node.children).strip()
            return '\n'.join(f'> {line}' if line else '>' for line in inner.split('\n'))
        if name == 'table':
            return cls._html_table_to_md(node)
        return cls._html_inline_to_md(node).strip()

    @classmethod
    def _html_list_to_md(cls, node: Tag, ordered: bool) -> str:
        """Convert an HTML list element to Markdown items.

        Args:
            node: Parsed HTML ``<ul>`` or ``<ol>`` element.
            ordered: True for ordered lists, False for unordered lists.

        Returns:
            Newline-joined Markdown list lines.
        """
        lines: list[str] = []
        for idx, li in enumerate(node.find_all('li', recursive=False)):
            prefix = f'{idx + 1}. ' if ordered else '- '
            lines.append(f'{prefix}{cls._html_inline_to_md(li).strip()}')
        return '\n'.join(lines)

    @classmethod
    def _html_table_to_md(cls, table: Tag) -> str:
        """Convert an HTML table tag into a GFM Markdown pipe table.

        Args:
            table: Parsed HTML ``<table>`` element.

        Returns:
            Markdown pipe table string, or empty string if no rows exist.
        """
        rows = table.find_all('tr')
        if not rows:
            return ''
        parsed_rows = [
            [cls._html_inline_to_md(cell).strip().replace('|', '\\|') for cell in row.find_all(['th', 'td'])]
            for row in rows
        ]
        parsed_rows = [r for r in parsed_rows if r]
        if not parsed_rows:
            return ''
        width = max(len(r) for r in parsed_rows)
        padded = [r + [''] * (width - len(r)) for r in parsed_rows]
        header = '| ' + ' | '.join(padded[0]) + ' |'
        sep = '| ' + ' | '.join(['---'] * width) + ' |'
        body = ['| ' + ' | '.join(row) + ' |' for row in padded[1:]]
        return '\n'.join([header, sep, *body])

    @classmethod
    def _html_inline_to_md(cls, node: PageElement) -> str:
        """Convert inline HTML tags to Markdown inline syntax.

        Args:
            node: Inline HTML DOM element or navigable text leaf.

        Returns:
            Inline Markdown text string.
        """
        if not isinstance(node, Tag):
            return str(node)
        name = node.name.lower()
        inner = ''.join(cls._html_inline_to_md(child) for child in node.children)
        if name in {'strong', 'b'}:
            return f'**{inner}**'
        if name in {'em', 'i'}:
            return f'*{inner}*'
        if name == 'code':
            return f'`{inner}`'
        if name == 'a':
            href = node.get('href')
            target = str(href) if href else ''
            return f'[{inner or target}]({target})' if target else inner
        if name == 'img':
            src = node.get('src')
            alt = node.get('alt')
            return f'![{str(alt) if alt else ""}]({str(src) if src else ""})'
        if name == 'br':
            return '\n'
        return inner
