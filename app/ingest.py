import logging
import re
from pathlib import Path
from typing import Any

from .config import config
from .db import connect, dumps, load_llm_settings
from .llm import embed_texts, summarize_source
from .vector_store import delete_source as delete_source_vectors
from .vector_store import upsert_chunks


ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".docx", ".html", ".htm", ".srt", ".vtt"}
logger = logging.getLogger(__name__)

# Inline WebVTT tags (e.g. <v Speaker>, <00:00:01.000>, <c.classname>) stripped
# from caption text so only the spoken words remain.
_VTT_INLINE_TAG = re.compile(r"<[^>]+>")


def supported(filename: str) -> bool:
    """Return whether the filename extension is accepted for ingestion."""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def extract_sections(path: Path) -> list[tuple[str, str]]:
    """Extract text sections from a supported source file.

    Dispatches per suffix to a helper. Each helper returns a list of
    ``(location, text)`` pairs; the location label flows through to chunk
    citations so users can see whether an answer came from the body, a
    header, a footnote, etc.
    """
    suffix = path.suffix.lower()
    logger.info("extract_started path=%s suffix=%s", path.name, suffix)
    if suffix == ".pdf":
        sections = _extract_pdf(path)
    elif suffix == ".docx":
        sections = _extract_docx(path)
    elif suffix in {".html", ".htm"}:
        sections = _extract_html(path)
    elif suffix in {".srt", ".vtt"}:
        sections = _extract_subtitles(path)
    else:
        sections = [("document", path.read_text(encoding="utf-8", errors="ignore"))]
    logger.info("extract_completed path=%s sections=%s", path.name, len(sections))
    return sections


def _extract_subtitles(path: Path) -> list[tuple[str, str]]:
    """Extract spoken text from an .srt / .vtt subtitle file (A7).

    Strips cue index numbers, timestamp lines, the WebVTT header and
    NOTE/STYLE/REGION metadata blocks, and inline VTT tags — leaving the
    caption text as a single ``transcript`` section. Consecutive duplicate
    lines (common with rolling captions) are collapsed. No new dependency.
    """
    raw = path.read_text(encoding="utf-8", errors="ignore")
    lines: list[str] = []
    skip_block = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            skip_block = False  # a blank line ends any NOTE/STYLE block
            continue
        # WebVTT header + metadata blocks run until the next blank line.
        if stripped == "WEBVTT" or stripped.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            skip_block = True
            continue
        if skip_block:
            continue
        if "-->" in stripped:  # timestamp cue line (SRT or VTT, incl. positioning)
            continue
        if stripped.isdigit():  # bare SRT cue index
            continue
        text = _VTT_INLINE_TAG.sub("", stripped).strip()
        if not text:
            continue
        if lines and lines[-1] == text:  # collapse rolling-caption repeats
            continue
        lines.append(text)
    transcript = "\n".join(lines)
    return [("transcript", transcript)] if transcript else []


def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    """Extract PDF paragraphs + tables in page reading order.

    Preferred path uses pdfplumber so paragraphs and tables can be emitted as
    interleaved blocks (``page N paragraph K`` / ``page N table M``). If
    pdfplumber is unavailable or fails for a document, falls back to plain
    pypdf text extraction.
    """
    from pypdf import PdfReader

    structured_sections = _extract_pdf_with_pdfplumber(path)
    if structured_sections:
        return structured_sections

    reader = PdfReader(str(path))
    return [
        (f"page {index}", page.extract_text() or "")
        for index, page in enumerate(reader.pages, start=1)
    ]


def _extract_pdf_with_pdfplumber(path: Path) -> list[tuple[str, str]]:
    """Best-effort structured PDF extraction using pdfplumber."""
    try:
        import pdfplumber
    except Exception:
        return []

    try:
        with pdfplumber.open(str(path)) as pdf:
            sections: list[tuple[str, str]] = []
            for page_index, page in enumerate(pdf.pages, start=1):
                sections.extend(_extract_pdf_page_blocks(page, page_index))
            return sections
    except Exception:
        logger.exception("pdfplumber_extract_failed path=%s", path.name)
        return []


def _extract_pdf_page_blocks(page, page_index: int) -> list[tuple[str, str]]:
    """Extract table + paragraph blocks from a page, preserving top-to-bottom order."""
    tables = list(page.find_tables() or [])
    table_bboxes = [table.bbox for table in tables if getattr(table, "bbox", None)]

    table_blocks: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables, start=1):
        table_text = _render_pdf_table(table.extract() or [])
        if not table_text:
            continue
        table_blocks.append(
            {
                "top": _pdf_bbox_top(getattr(table, "bbox", None)),
                "location": f"page {page_index} table {table_index}",
                "text": table_text,
            }
        )

    words = list(page.extract_words() or [])
    non_table_words = [
        word for word in words if not _pdf_word_in_any_bbox(word, table_bboxes)
    ]
    paragraph_blocks = _pdf_words_to_paragraph_blocks(non_table_words, page_index)

    merged = [
        {"kind": "paragraph", **block}
        for block in paragraph_blocks
    ] + [
        {"kind": "table", **block}
        for block in table_blocks
    ]
    merged.sort(key=lambda block: (block["top"], 0 if block["kind"] == "paragraph" else 1))

    return [
        (block["location"], block["text"])
        for block in merged
        if block["text"].strip()
    ]


def _pdf_bbox_top(bbox: tuple[float, float, float, float] | None) -> float:
    if not bbox:
        return 0.0
    return float(bbox[1])


def _pdf_word_in_any_bbox(
    word: dict[str, Any],
    bboxes: list[tuple[float, float, float, float]],
) -> bool:
    x0 = float(word.get("x0", 0.0))
    x1 = float(word.get("x1", x0))
    top = float(word.get("top", 0.0))
    bottom = float(word.get("bottom", top))
    cx = (x0 + x1) / 2.0
    cy = (top + bottom) / 2.0
    for bx0, btop, bx1, bbottom in bboxes:
        if bx0 <= cx <= bx1 and btop <= cy <= bbottom:
            return True
    return False


def _pdf_words_to_paragraph_blocks(words: list[dict[str, Any]], page_index: int) -> list[dict[str, Any]]:
    """Group non-table words into paragraph blocks by line and vertical gap."""
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (float(w.get("top", 0.0)), float(w.get("x0", 0.0))))
    line_tolerance = 3.0
    lines: list[dict[str, Any]] = []
    current_line: list[dict[str, Any]] = []
    line_top = 0.0

    def flush_line() -> None:
        nonlocal current_line, line_top
        if not current_line:
            return
        current_line.sort(key=lambda w: float(w.get("x0", 0.0)))
        text = " ".join((w.get("text") or "").strip() for w in current_line if (w.get("text") or "").strip())
        if text:
            tops = [float(w.get("top", line_top)) for w in current_line]
            bottoms = [float(w.get("bottom", line_top)) for w in current_line]
            lines.append(
                {
                    "top": min(tops),
                    "bottom": max(bottoms),
                    "text": text,
                }
            )
        current_line = []

    for word in sorted_words:
        word_top = float(word.get("top", 0.0))
        if not current_line:
            current_line = [word]
            line_top = word_top
            continue
        if abs(word_top - line_top) <= line_tolerance:
            current_line.append(word)
        else:
            flush_line()
            current_line = [word]
            line_top = word_top
    flush_line()

    paragraph_gap = 12.0
    paragraphs: list[dict[str, Any]] = []
    para_lines: list[str] = []
    para_top = 0.0
    previous_bottom = 0.0
    paragraph_index = 1

    def flush_paragraph() -> None:
        nonlocal para_lines, para_top, paragraph_index
        if not para_lines:
            return
        text = "\n".join(para_lines).strip()
        if text:
            paragraphs.append(
                {
                    "top": para_top,
                    "location": f"page {page_index} paragraph {paragraph_index}",
                    "text": text,
                }
            )
            paragraph_index += 1
        para_lines = []

    for line in lines:
        if not para_lines:
            para_top = float(line["top"])
            para_lines = [line["text"]]
            previous_bottom = float(line["bottom"])
            continue
        gap = float(line["top"]) - previous_bottom
        if gap > paragraph_gap:
            flush_paragraph()
            para_top = float(line["top"])
            para_lines = [line["text"]]
        else:
            para_lines.append(line["text"])
        previous_bottom = float(line["bottom"])
    flush_paragraph()

    return paragraphs


def _render_pdf_table(rows: list[list[Any]]) -> str:
    """Render extracted table rows into retrieval-friendly pipe-separated text."""
    cleaned_rows: list[list[str]] = []
    for row in rows:
        cleaned = [" ".join(str(cell or "").split()) for cell in row]
        if any(cell for cell in cleaned):
            cleaned_rows.append(cleaned)
    if not cleaned_rows:
        return ""
    return "Table:\n" + "\n".join(" | ".join(row) for row in cleaned_rows)


def _extract_docx(path: Path) -> list[tuple[str, str]]:
    """Extract paragraphs, tables, headers, footers, text boxes, footnotes.

    The historical implementation only walked ``doc.paragraphs``, silently
    dropping every cell in every table (which in a typical case-study docx
    is 99% of the content). This walks the document body in order and
    flattens tables inline (with nested-table recursion), then emits
    headers / footers / text boxes / footnotes as their own labelled
    sections so citations can tell users where evidence came from.
    """
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(path))
    sections: list[tuple[str, str]] = []

    body_text = _render_docx_container(doc)
    if body_text.strip():
        sections.append(("document", body_text))

    # Headers / footers are in their own XML parts, not in doc.element.body.
    header_chunks, footer_chunks = [], []
    for section in doc.sections:
        h = _render_docx_container(section.header)
        f = _render_docx_container(section.footer)
        if h.strip():
            header_chunks.append(h)
        if f.strip():
            footer_chunks.append(f)
    if header_chunks:
        sections.append(("header", "\n\n".join(header_chunks)))
    if footer_chunks:
        sections.append(("footer", "\n\n".join(footer_chunks)))

    # Text boxes (<w:txbxContent>) live inside drawings and are skipped by
    # the body's CT_P / CT_Tbl iteration. Flatten any w:t runs we find.
    txbx_chunks = []
    for txbx in doc.element.iter(qn("w:txbxContent")):
        text = " ".join(t.text for t in txbx.iter(qn("w:t")) if t.text)
        if text.strip():
            txbx_chunks.append(text)
    if txbx_chunks:
        sections.append(("text boxes", "\n\n".join(txbx_chunks)))

    # Footnotes / endnotes are stored as separate package parts referenced
    # from the document part by relationship type. Each w:t inside the part
    # is a footnote body run; we just concatenate.
    note_chunks = []
    for rel in doc.part.rels.values():
        if "footnote" in rel.reltype or "endnote" in rel.reltype:
            try:
                root = rel.target_part.element
            except AttributeError:
                continue
            text = " ".join(t.text for t in root.iter(qn("w:t")) if t.text)
            if text.strip():
                note_chunks.append(text)
    if note_chunks:
        sections.append(("footnotes", "\n\n".join(note_chunks)))

    return sections


def _iter_docx_block_items(parent):
    """Yield Paragraph and Table children of parent in document order.

    Works for Document (the body), _Cell (cell contents, for nested tables),
    and _Header / _Footer (their own XML root). Order matters: python-docx's
    ``parent.paragraphs`` and ``parent.tables`` each return a flat list, so
    a doc that alternates paragraphs and tables would lose its narrative
    flow if you combined them naively.
    """
    from docx.document import Document as _Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import _Cell, Table
    from docx.text.paragraph import Paragraph

    if isinstance(parent, _Document):
        parent_elem = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elem = parent._tc
    elif hasattr(parent, "_element"):
        # _Header / _Footer
        parent_elem = parent._element
    else:
        parent_elem = parent

    for child in parent_elem.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _render_docx_container(container) -> str:
    """Render a Document / _Header / _Footer / _Cell as text with tables inline."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parts: list[str] = []
    for block in _iter_docx_block_items(container):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                parts.append(text)
        elif isinstance(block, Table):
            rendered = _render_docx_table(block)
            if rendered:
                parts.append(rendered)
    return "\n".join(parts)


def _render_docx_table(table) -> str:
    """Render a table as ``Table:`` plus ' | '-separated cells per row.

    Nested tables (a table inside a cell) are rendered inline by recursing
    through ``_render_docx_container`` on each cell. Whitespace inside a
    cell is collapsed to single spaces so the row separator stays
    unambiguous.
    """
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            cell_text = _render_docx_container(cell)
            cells.append(" ".join(cell_text.split()))
        if any(c.strip() for c in cells):
            rows.append(" | ".join(cells))
    if not rows:
        return ""
    return "Table:\n" + "\n".join(rows)


def _extract_html(path: Path) -> list[tuple[str, str]]:
    """Strip noise + recover alt / title / meta-description text BeautifulSoup
    would otherwise drop. Returns a single section.

    What changed vs the naive ``soup.get_text``:
      - script / style / noscript / template removed (noise).
      - elements with ``hidden`` attribute or inline ``style='display:none'``
        removed (catches injected honeypots / hidden JSON-LD blocks).
      - <meta name='description'> and og:description appended (cheap recall).
      - <img alt> / <a title> / <input value> appended as ``[image: ...]``
        style sidecar lines so they survive get_text() without polluting
        the main flow.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    # Hidden via attribute or inline style. We deliberately do NOT touch
    # aria-hidden, visibility:hidden, or CSS-class-based hiding — those
    # often mark legitimate collapsed content (tabs, accordions) that the
    # user can still expand to read.
    for element in soup.find_all(hidden=True):
        element.decompose()
    for element in soup.find_all(style=True):
        style = element.get("style", "").lower().replace(" ", "")
        if "display:none" in style:
            element.decompose()

    extras: list[str] = []
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        extras.append(meta_desc["content"].strip())
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc and og_desc.get("content"):
        extras.append(og_desc["content"].strip())
    for img in soup.find_all("img", alt=True):
        alt = (img.get("alt") or "").strip()
        if alt:
            extras.append(f"[image: {alt}]")
    for anchor in soup.find_all("a", title=True):
        title = (anchor.get("title") or "").strip()
        if title:
            extras.append(f"[link: {title}]")
    for inp in soup.find_all("input", value=True):
        if inp.get("type") in {"hidden", "password", "submit", "button"}:
            continue
        value = (inp.get("value") or "").strip()
        if value:
            extras.append(f"[input: {value}]")

    body_text = soup.get_text("\n")
    if extras:
        body_text = body_text + "\n\n" + "\n".join(extras)
    return [("html", body_text)]


# Sentence boundary regex. Matches the END position after a terminator:
#   - CJK terminators (。！？): no trailing-space requirement, since CJK
#     doesn't space-separate sentences.
#   - Latin terminators (.!?): must be followed by whitespace or end of
#     input, to avoid splitting decimals (3.14), URLs, and most abbreviations.
#   - One or more newlines: always a boundary.
# Known limitation: "Mr. Smith" still splits at "Mr.". Acceptable for POC.
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？]+|[.!?](?=\s|$)|\n+")
# Soft punctuation used as a fallback when a single sentence is longer than
# the target chunk size. Includes both CJK and Latin commas / semicolons.
_SOFT_BREAK_RE = re.compile(r"[，、；,;]")
_CJK_RE = re.compile(r"[一-鿿]")
# Internal whitespace normalisation: collapse runs but preserve newlines so
# split_sentences() can use them as boundaries. PDFs often inject erratic
# spacing inside paragraphs that we still want flattened.
_HORIZONTAL_WS_RE = re.compile(r"[ \t\r\f\v]+")

# Chunking targets (config-driven; changing them requires re-indexing).
LATIN_TARGET_CHARS = config.chunking.latin_target_chars
CJK_TARGET_CHARS = config.chunking.cjk_target_chars
DEFAULT_OVERLAP_SENTENCES = config.chunking.overlap_sentences


def is_mostly_cjk(text: str, threshold: float = 0.30) -> bool:
    """Return True when CJK characters dominate the text (>= threshold).

    CJK and Latin script have very different character density (one CJK char
    carries roughly two English words of meaning), so chunk-size targets and
    sentence splitting both branch on this signal.
    """
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk / max(1, len(text)) >= threshold


def split_sentences(text: str) -> list[str]:
    """Split text into trimmed sentences, keeping the terminator punctuation."""
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        end = match.end()
        piece = text[start:end].strip()
        if piece:
            sentences.append(piece)
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _split_long_sentence(sentence: str, target_chars: int) -> list[str]:
    """Break a sentence that exceeds the chunk target.

    Tries soft punctuation (commas, semicolons) first; if even that leaves
    pieces too large (e.g. a wall of CJK with no internal punctuation), hard
    cuts at target_chars boundaries. Output pieces are <= target_chars.
    """
    pieces: list[str] = []
    buf = ""
    for fragment in _SOFT_BREAK_RE.split(sentence):
        candidate = buf + fragment
        if len(candidate) >= target_chars:
            if buf.strip():
                pieces.append(buf.strip())
            buf = fragment
        else:
            buf = candidate
    if buf.strip():
        pieces.append(buf.strip())

    # Any piece still over budget gets hard-cut. This is the worst case but
    # ensures we never feed a >>target_chars chunk to the embedding API.
    final: list[str] = []
    for piece in pieces:
        while len(piece) > target_chars:
            final.append(piece[:target_chars].strip())
            piece = piece[target_chars:]
        if piece.strip():
            final.append(piece.strip())
    return final


def _section_kind(location: str) -> str:
    """Classify extractor locations so unrelated section kinds do not merge."""
    label = (location or "").lower()
    if " table" in f" {label}" or label.startswith("table"):
        return "table"
    if "header" in label:
        return "header"
    if "footer" in label:
        return "footer"
    if "footnote" in label or "endnote" in label:
        return "footnote"
    if "text box" in label:
        return "text_box"
    if "transcript" in label:
        return "transcript"
    return "body"


def _span_label(locations: list[str]) -> str:
    """Build a citation label for a chunk that may span several sections.

    A chunk packed from one section keeps that section's location verbatim. A
    chunk that merged consecutive sections (e.g. several PDF paragraph blocks
    filled up to the chunk target) is labelled as a first-to-last span so the
    citation still points at the right region. Empty locations are ignored.
    """
    cleaned = [loc for loc in locations if loc]
    if not cleaned:
        return ""
    first, last = cleaned[0], cleaned[-1]
    return first if first == last else f"{first} – {last}"


def chunk_sections(
    sections: list[tuple[str, str]],
    target_chars: int | None = None,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[tuple[str, str]]:
    """Sentence-aware chunking that packs sentences across sections.

    Same sentence-aware strategy as :func:`chunk_text` (CJK-aware sizing,
    sentence-level overlap, long-sentence fallback), but it fills each chunk up
    to ``target_chars`` worth of sentences **across consecutive sections**
    instead of resetting at every section boundary. Without this, formats whose
    extractor emits many small sections — notably the PDF path's per-paragraph
    blocks (``page N paragraph K``) — leave each short paragraph as its own
    tiny fragment, while single-section formats (TXT/MD) fill to target. Each
    sentence carries its originating ``location`` so the emitted chunk is
    labelled with the source span it covers (see :func:`_span_label`).

    Returns ``(location, chunk_text)`` pairs in document order.
    """
    # Normalise each section's text the same way chunk_text does (collapse the
    # horizontal whitespace PDFs scatter mid-paragraph, keep newlines).
    normalized_sections: list[tuple[str, str]] = []
    for location, text in sections:
        if not text:
            continue
        normalized = _HORIZONTAL_WS_RE.sub(" ", text).strip()
        if normalized:
            normalized_sections.append((location, normalized))
    if not normalized_sections:
        return []

    if target_chars is None:
        combined = "\n".join(text for _, text in normalized_sections)
        target_chars = CJK_TARGET_CHARS if is_mostly_cjk(combined) else LATIN_TARGET_CHARS

    # Flatten to (location, sentence, section_kind) units across all sections.
    units: list[tuple[str, str, str]] = []
    for location, text in normalized_sections:
        kind = _section_kind(location)
        for sentence in split_sentences(text):
            units.append((location, sentence, kind))
    if not units:
        return []

    chunks: list[tuple[str, str]] = []
    current: list[tuple[str, str, str]] = []  # (location, sentence, section_kind)
    current_len = 0

    def flush() -> None:
        if current:
            body = " ".join(sentence for _, sentence, _ in current).strip()
            if body:
                chunks.append((_span_label([loc for loc, _, _ in current]), body))

    for location, sentence, kind in units:
        if current and kind != current[-1][2]:
            flush()
            # Do not carry overlap across body/table/header/footer/etc.; it
            # pollutes citations and can glue unrelated extractor regions.
            current = []
            current_len = 0

        if len(sentence) > target_chars:
            flush()
            # Carry-over does not apply across an over-long sentence — by
            # definition it already contains too much context.
            current = []
            current_len = 0
            for piece in _split_long_sentence(sentence, target_chars):
                if piece:
                    chunks.append((location, piece))
            continue

        if current and current_len + len(sentence) + 1 > target_chars:
            flush()
            if overlap_sentences > 0:
                current = current[-overlap_sentences:]
                current_len = sum(len(s) + 1 for _, s, _ in current)
                # If carrying overlap would make the next chunk exceed the
                # target, drop the overlap. This keeps e5-sized CJK chunks from
                # doubling up around dense boundary sentences.
                if current and current_len + len(sentence) + 1 > target_chars:
                    current = []
                    current_len = 0
            else:
                current = []
                current_len = 0

        current.append((location, sentence, kind))
        current_len += len(sentence) + 1

    flush()
    return [(loc, body) for loc, body in chunks if body]


def chunk_text(
    text: str,
    target_chars: int | None = None,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[str]:
    """Split a single text into sentence-aware retrieval chunks.

    Strategy:
        1. Normalise horizontal whitespace, keep newlines as boundaries.
        2. Detect CJK-dominance to pick a chunk size (Chinese carries roughly
           2x the information density per character of English, so CJK chunks
           target half the chars).
        3. Split into sentences using ``。！？!?\\n`` as primary boundaries.
        4. Greedily fill chunks up to ``target_chars`` worth of sentences.
        5. Overlap chunks by carrying the last ``overlap_sentences`` sentences
           into the next chunk (sentence-level overlap, not char-level —
           preserves grammar at chunk boundaries).
        6. A single sentence longer than ``target_chars`` is split further by
           soft punctuation, then hard-cut as a last resort.

    Pass ``target_chars=None`` (the default) to auto-pick from text language.
    Thin wrapper over :func:`chunk_sections` for a single unlabelled section.
    """
    return [body for _, body in chunk_sections([("", text)], target_chars, overlap_sentences)]


def get_settings() -> dict[str, Any]:
    """Load the single global LLM settings row with the API key decrypted."""
    with connect() as conn:
        return load_llm_settings(conn) or {}


async def _generate_source_summary(source_id: int) -> None:
    """Generate and persist a per-source TL;DR. Failures are logged only."""
    try:
        with connect() as conn:
            source = conn.execute("SELECT user_id, notebook_id FROM sources WHERE id = ?", (source_id,)).fetchone()
            chunk_rows = conn.execute(
                "SELECT location, text FROM chunks WHERE source_id = ? ORDER BY chunk_index ASC LIMIT 12",
                (source_id,),
            ).fetchall()
            settings = load_llm_settings(conn) or {}
        chunks = [dict(r) for r in chunk_rows]
        if not chunks:
            return
        usage_context = {
            "source_id": source_id,
            "user_id": source["user_id"] if source else None,
            "notebook_id": source["notebook_id"] if source else None,
        }
        summary = await summarize_source(chunks, settings, usage_context=usage_context)
        if not summary:
            return
        with connect() as conn:
            conn.execute(
                "UPDATE sources SET summary = ?, summary_at = CURRENT_TIMESTAMP WHERE id = ?",
                (summary, source_id),
            )
        logger.info("source_summary_persisted source_id=%s chars=%s", source_id, len(summary))
    except Exception:
        logger.exception("source_summary_unhandled source_id=%s", source_id)


async def process_source(source_id: int) -> None:
    """Extract, chunk, embed, and persist vectors for one source record."""
    with connect() as conn:
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            logger.warning("ingest_source_missing source_id=%s", source_id)
            return
        conn.execute("UPDATE sources SET status = 'processing', error = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (source_id,))
        conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    try:
        delete_source_vectors(source_id, source["user_id"])
    except Exception:
        logger.exception("vector_source_delete_failed source_id=%s", source_id)

    try:
        logger.info(
            "ingest_started source_id=%s user_id=%s filename=%s",
            source_id,
            source["user_id"],
            source["filename"],
        )
        sections = extract_sections(Path(source["stored_path"]))
        # Pack sentences across sections up to the chunk target so formats that
        # emit many small sections (PDF per-paragraph blocks) produce the same
        # well-sized chunks as single-section formats (TXT/MD) rather than
        # hundreds of tiny fragments.
        records: list[tuple[str, str]] = chunk_sections(sections)
        if not records:
            raise ValueError("No extractable text found.")

        logger.info(
            "ingest_chunked source_id=%s sections=%s chunks=%s",
            source_id,
            len(sections),
            len(records),
        )
        embeddings = await embed_texts(
            [text for _, text in records],
            get_settings(),
            role="passage",
            usage_context={"user_id": source["user_id"], "notebook_id": source["notebook_id"], "source_id": source_id},
        )
        with connect() as conn:
            chunk_rows = [
                (source["user_id"], source_id, index, location, text, dumps(embedding))
                for index, ((location, text), embedding) in enumerate(zip(records, embeddings))
            ]
            conn.executemany(
                """
                INSERT INTO chunks (user_id, source_id, chunk_index, location, text, embedding_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                chunk_rows,
            )
            inserted = conn.execute(
                """
                SELECT chunks.*, sources.filename
                FROM chunks JOIN sources ON sources.id = chunks.source_id
                WHERE chunks.source_id = ?
                ORDER BY chunks.chunk_index
                """,
                (source_id,),
            ).fetchall()
            conn.execute(
                "UPDATE sources SET status = 'indexed', error = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (source_id,),
            )
        upsert_chunks(
            [
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "source_id": row["source_id"],
                    "chunk_index": row["chunk_index"],
                    "filename": row["filename"],
                    "location": row["location"],
                    "text": row["text"],
                    "embedding": embeddings[row["chunk_index"]],
                }
                for row in inserted
            ]
        )
        logger.info("ingest_completed source_id=%s chunks=%s", source_id, len(records))
        # Best-effort per-source summary. Runs AFTER status='indexed' so a
        # summarization failure leaves the source fully usable for retrieval.
        await _generate_source_summary(source_id)
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                """
                UPDATE sources
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(exc)[:500], source_id),
            )
        logger.exception("ingest_failed source_id=%s", source_id)
