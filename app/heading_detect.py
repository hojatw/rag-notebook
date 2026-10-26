"""A12 Phase 1 — document heading detection, diagnostics only.

`detect_structure` looks for a document's heading hierarchy with a cascade of
independent signals, strongest first, and stops at the first one that yields a
usable result:

1. ``outline`` — structure the author declared: PDF bookmarks, DOCX
   ``Heading N`` / ``標題 N`` styles, HTML ``h1``–``h6``.
2. ``toc_page`` — a PDF page that reads as a table of contents (title, dot
   leaders or spacing, ascending page numbers). Consecutive TOC pages are
   merged into one entry list.
3. ``numbering`` — body lines that start with a heading number (``5.2.1``,
   ``壹、`` / ``一、`` / ``（一）``, ``第三章`` / ``第 12 條``, ``Chapter 4``).
4. ``none`` — nothing usable. This is the expected tier for a large share of
   the customer corpus (see docs/ROADMAP.md A12), not an edge case.

Font size is deliberately **not** a tier: Phase 0 measured it near zero on
every corpus (headings share the body font and differ only by weight).

Phase 1 scope: the result only feeds the A6a diagnostics panel via
``ExtractionResult.details["structure"]``. It never changes sections, chunks or
the index, so every tier — ``none`` included — leaves ingest output identical
to before. Each layer swallows its own errors and falls through, because a
detector bug must never fail an ingest.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TIER_OUTLINE = "outline"
TIER_TOC_PAGE = "toc_page"
TIER_NUMBERING = "numbering"
TIER_NONE = "none"

# Thresholds from the Phase 0 measurement. An outline with one or two nodes
# was common and is not a usable tree, so count the nodes.
MIN_OUTLINE_NODES = 3
MIN_TOC_LINES = 5
MIN_TOC_CONTINUATION_LINES = 2
MIN_NUMBERED_HEADINGS = 3
MAX_HEADING_CHARS = 40

_CN_NUM = "一二三四五六七八九十百"
_ZH_UPPER = "壹貳參叁肆伍陸柒捌玖拾"
# Order matters only for depth: the zh-TW official-document sequence is
# 壹、 → 一、 → （一） → 1. → （1）.
_DEPTH_PATTERNS: list[tuple[re.Pattern[str], int | None]] = [
    (re.compile(rf"^第\s*[{_CN_NUM}\d]+\s*[篇部]"), 1),
    (re.compile(rf"^第\s*[{_CN_NUM}\d]+\s*章"), 1),
    (re.compile(rf"^第\s*[{_CN_NUM}\d]+\s*節"), 2),
    (re.compile(rf"^第\s*[{_CN_NUM}\d]+\s*(-\s*\d+\s*)?條"), 3),
    (re.compile(rf"^[{_ZH_UPPER}]+[、．.]"), 1),
    (re.compile(rf"^[{_CN_NUM}]+[、．]"), 2),
    (re.compile(rf"^[（(][{_CN_NUM}]+[）)]"), 3),
    (re.compile(r"^[（(]\d{1,2}[）)]"), 5),
    (re.compile(r"^(Chapter|CHAPTER)\s+\d+"), 1),
    (re.compile(r"^(Section|SECTION)\s+\d+"), 2),
    # 5.2.1 Title / 5.2.1. Title / 1. Title — depth is the component count.
    (re.compile(r"^\d{1,2}(\.\d{1,2}){0,4}\.?\s+\S"), None),
]
_DOTTED_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})")
_TOC_LINE_RE = re.compile(
    r"^(?P<title>.{2,80}?)\s*(?:[.．·…_\-\s]{3,})\s*(?P<page>\d{1,4})\s*$"
)
_PAGE_RE = re.compile(r"^page (\d+)")
_DOCX_HEADING_RE = re.compile(r"^(?:Heading|標題)\s*(\d)")


@dataclasses.dataclass
class Heading:
    title: str
    depth: int


@dataclasses.dataclass
class StructureResult:
    tier: str
    headings: list[Heading] = dataclasses.field(default_factory=list)
    #: PDF page numbers detected as table-of-contents pages. Phase 2 must not
    #: index these as body text, or their entries pollute retrieval.
    toc_pages: list[int] = dataclasses.field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """The compact, content-free shape stored in ``diagnostics_json``."""
        return {
            "tier": self.tier,
            "headings": len(self.headings),
            # Distinct levels, not the largest number: a document numbered
            # 一、 → （一） has two levels even though （一） maps to depth 3.
            "levels": len({h.depth for h in self.headings}),
            "toc_pages": self.toc_pages,
        }


def numbering_depth(title: str) -> int | None:
    """Heading depth implied by a title's leading number, or None."""
    text = title.strip()
    for pattern, depth in _DEPTH_PATTERNS:
        if pattern.match(text):
            if depth is None:
                return _DOTTED_RE.match(text).group(1).count(".") + 1
            return depth
    return None


def _flatten_pdf_outline(items: list, depth: int, out: list[Heading]) -> None:
    for item in items:
        if isinstance(item, list):
            _flatten_pdf_outline(item, depth + 1, out)
            continue
        title = (getattr(item, "title", None) or "").strip()
        if title:
            out.append(Heading(title, depth))


def _outline_headings(path: Path) -> list[Heading]:
    suffix = path.suffix.lower()
    headings: list[Heading] = []
    if suffix == ".pdf":
        from pypdf import PdfReader

        _flatten_pdf_outline(PdfReader(str(path)).outline, 1, headings)
    elif suffix == ".docx":
        from docx import Document

        for paragraph in Document(str(path)).paragraphs:
            style = paragraph.style.name if paragraph.style is not None else ""
            match = _DOCX_HEADING_RE.match(style or "")
            if match and paragraph.text.strip():
                headings.append(Heading(paragraph.text.strip(), int(match.group(1))))
    elif suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            title = tag.get_text(" ", strip=True)
            if title:
                headings.append(Heading(title, int(tag.name[1])))
    return headings


def _lines(sections: list[tuple[str, str]]):
    for location, text in sections:
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield location, line


def _toc_headings(sections: list[tuple[str, str]]) -> tuple[list[Heading], list[int]]:
    by_page: dict[int, list[tuple[str, int]]] = {}
    for location, line in _lines(sections):
        page = _PAGE_RE.match(location)
        match = _TOC_LINE_RE.match(line)
        if not page or not match or match.group("title").strip().isdigit():
            continue
        by_page.setdefault(int(page.group(1)), []).append(
            (match.group("title").strip(), int(match.group("page")))
        )
    toc_pages: list[int] = []
    for page_no in sorted(by_page):
        entries = by_page[page_no]
        # A continuation page right after a TOC page is often short (the
        # table of contents just ran over), so it only needs a couple of lines.
        continues = bool(toc_pages) and toc_pages[-1] == page_no - 1
        if len(entries) < (MIN_TOC_CONTINUATION_LINES if continues else MIN_TOC_LINES):
            continue
        numbers = [n for _, n in entries]
        ascending = sum(1 for a, b in zip(numbers, numbers[1:]) if b >= a)
        if ascending >= 0.8 * (len(numbers) - 1):
            toc_pages.append(page_no)
    # Consecutive TOC pages are one table of contents: merge them in order.
    headings = [
        Heading(title, numbering_depth(title) or 1)
        for page_no in toc_pages
        for title, _ in by_page[page_no]
    ]
    return headings, toc_pages


def _numbered_headings(sections: list[tuple[str, str]]) -> list[Heading]:
    headings: list[Heading] = []
    seen: set[str] = set()
    for _, line in _lines(sections):
        if len(line) > MAX_HEADING_CHARS or _TOC_LINE_RE.match(line):
            continue
        depth = numbering_depth(line)
        # A repeated line is a running header, not a heading.
        if depth is not None and line not in seen:
            seen.add(line)
            headings.append(Heading(line, depth))
    return headings


def detect_structure(path: Path, sections: list[tuple[str, str]]) -> StructureResult:
    """Run the cascade; never raises."""
    try:
        outline = _outline_headings(path)
    except Exception:
        logger.warning("structure_outline_failed path=%s", path.name, exc_info=True)
        outline = []
    if len(outline) >= MIN_OUTLINE_NODES:
        return StructureResult(TIER_OUTLINE, outline)
    try:
        toc, toc_pages = _toc_headings(sections)
    except Exception:
        logger.warning("structure_toc_failed path=%s", path.name, exc_info=True)
        toc, toc_pages = [], []
    if len(toc) >= MIN_TOC_LINES:
        return StructureResult(TIER_TOC_PAGE, toc, toc_pages)
    try:
        numbered = _numbered_headings(sections)
    except Exception:
        logger.warning("structure_numbering_failed path=%s", path.name, exc_info=True)
        numbered = []
    if len(numbered) >= MIN_NUMBERED_HEADINGS:
        return StructureResult(TIER_NUMBERING, numbered)
    return StructureResult(TIER_NONE)
