"""A small, safe Markdown-to-HTML renderer for the guides the browser app shows.

It covers what ``docs/USER_GUIDE.md`` and ``docs/ASSUMPTION_GLOSSARY.md`` use:
ATX headings (with GitHub-style anchors), paragraphs, nested bullet and
numbered lists, fenced code, pipe tables, block quotes, horizontal rules,
and inline code, bold, italics and links. Everything is HTML-escaped first;
raw HTML in the source (including comments) is shown as text or dropped,
never passed through. Links are kept only when they are in-page anchors,
known guide documents (rewritten by ``link_map``) or ``http(s)`` URLs (opened
in a new tab with ``rel="noopener noreferrer"``); anything else (``javascript:``,
relative paths to files the app does not serve) becomes plain text.
"""

from __future__ import annotations

import re
from html import escape
from typing import Callable, Mapping, Optional

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")

LinkMap = Callable[[str], Optional[str]]


def slugify(text: str) -> str:
    """GitHub-style heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _safe_href(href: str, link_map: Optional[LinkMap]) -> tuple[Optional[str], bool]:
    """(href to emit, external?) or (None, False) when the link is dropped."""
    href = href.strip()
    if href.startswith("#"):
        return href, False
    if link_map is not None:
        mapped = link_map(href)
        if mapped is not None:
            return mapped, False
    if re.match(r"^https?://[^\s\"'<>]+$", href, re.I):
        return href, True
    return None, False


def _inline(text: str, link_map: Optional[LinkMap]) -> str:
    """Render inline Markdown in one line of (unescaped) source text."""
    out: list[str] = []
    pos = 0
    pattern = re.compile(r"`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)")
    for match in pattern.finditer(text):
        out.append(_emphasis(escape(text[pos:match.start()], quote=False)))
        if match.group(1) is not None:
            out.append(f"<code>{escape(match.group(1), quote=False)}</code>")
        else:
            label = _emphasis(escape(match.group(2), quote=False))
            label = re.sub(r"`([^`]+)`", r"<code>\1</code>", label)
            href, external = _safe_href(match.group(3), link_map)
            if href is None:
                out.append(label)
            elif external:
                out.append(f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>')
            else:
                out.append(f'<a href="{escape(href, quote=True)}">{label}</a>')
        pos = match.end()
    out.append(_emphasis(escape(text[pos:], quote=False)))
    return "".join(out)


def _emphasis(escaped: str) -> str:
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", escaped)
    return escaped


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)]


def render_markdown(source: str, *, link_map: Optional[LinkMap] = None) -> str:
    """HTML for a Markdown document (safe to insert into a page)."""
    lines = source.replace("\r\n", "\n").split("\n")
    html: list[str] = []
    i = 0
    n = len(lines)

    def paragraph_end(line: str) -> bool:
        return (not line.strip() or _HEADING.match(line) is not None or _FENCE.match(line) is not None
                or _LIST.match(line) is not None or line.lstrip().startswith(("|", ">"))
                or _HR.match(line) is not None)

    while i < n:
        line = lines[i]
        if not line.strip() or _COMMENT.match(line):
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            body: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1
            html.append(f"<pre><code>{escape(chr(10).join(body), quote=False)}</code></pre>")
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            html.append(f'<h{level} id="{escape(slugify(text), quote=True)}">{_inline(text, link_map)}</h{level}>')
            i += 1
            continue
        if _HR.match(line):
            html.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            head = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            parts = ['<div class="mdtable"><table><thead><tr>']
            parts += [f"<th>{_inline(cell, link_map)}</th>" for cell in head]
            parts.append("</tr></thead><tbody>")
            for row in rows:
                parts.append("<tr>" + "".join(f"<td>{_inline(cell, link_map)}</td>" for cell in row) + "</tr>")
            parts.append("</tbody></table></div>")
            html.append("".join(parts))
            continue
        if line.lstrip().startswith(">"):
            quoted: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                quoted.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            html.append(f"<blockquote>{render_markdown(chr(10).join(quoted), link_map=link_map)}</blockquote>")
            continue
        if _LIST.match(line):
            block: list[str] = []
            while i < n and (lines[i].strip() == "" or _LIST.match(lines[i]) or lines[i].startswith((" ", "\t"))):
                if lines[i].strip() == "" and (i + 1 >= n or not (_LIST.match(lines[i + 1]) or
                                                                  lines[i + 1].startswith((" ", "\t")))):
                    break
                block.append(lines[i])
                i += 1
            html.append(_render_list(block, link_map))
            continue
        para = [line.strip()]
        i += 1
        while i < n and not paragraph_end(lines[i]):
            para.append(lines[i].strip())
            i += 1
        html.append(f"<p>{_inline(' '.join(para), link_map)}</p>")
    return "\n".join(html)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _render_list(block: list[str], link_map: Optional[LinkMap]) -> str:
    """A (possibly nested) list from its lines; continuation lines join the item above."""
    items: list[tuple[int, bool, str]] = []  # (indent, ordered, text)
    for raw in block:
        if not raw.strip():
            continue
        match = _LIST.match(raw)
        if match:
            items.append((len(match.group(1).expandtabs(2)), match.group(2)[0].isdigit(), match.group(3)))
        elif items:
            indent, ordered, text = items[-1]
            items[-1] = (indent, ordered, f"{text} {raw.strip()}")
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)
    for indent, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0] and tag != stack[-1][1]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}><li>")
            stack.append((indent, tag))
        else:
            out.append("</li><li>")
        out.append(_inline(text, link_map))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def guide_link_map(routes: Mapping[str, str]) -> LinkMap:
    """A link_map that rewrites ``NAME.md`` / ``NAME.md#anchor`` links to app routes."""

    def mapped(href: str) -> Optional[str]:
        target, _, anchor = href.partition("#")
        name = target.rsplit("/", 1)[-1]
        route = routes.get(name)
        if route is None:
            return None
        return f"{route}/{anchor}" if anchor else route

    return mapped
