import logging
import re
from pathlib import Path
from typing import Any

from .db import connect, dumps, load_llm_settings
from .llm import embed_texts
from .vector_store import delete_source as delete_source_vectors
from .vector_store import upsert_chunks


ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".docx", ".html", ".htm"}
logger = logging.getLogger(__name__)


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
    else:
        sections = [("document", path.read_text(encoding="utf-8", errors="ignore"))]
    logger.info("extract_completed path=%s sections=%s", path.name, len(sections))
    return sections


def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    """Page-by-page text extraction. Scanned pages return '' (no OCR)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return [
        (f"page {index}", page.extract_text() or "")
        for index, page in enumerate(reader.pages, start=1)
    ]


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

LATIN_TARGET_CHARS = 800
CJK_TARGET_CHARS = 400
DEFAULT_OVERLAP_SENTENCES = 1


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


def chunk_text(
    text: str,
    target_chars: int | None = None,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[str]:
    """Split text into sentence-aware retrieval chunks.

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
    """
    if not text:
        return []
    # Preserve newlines (sentence boundaries) but collapse internal runs of
    # spaces, tabs and form-feeds that PDFs love to scatter mid-paragraph.
    normalized = _HORIZONTAL_WS_RE.sub(" ", text).strip()
    if not normalized:
        return []

    if target_chars is None:
        target_chars = CJK_TARGET_CHARS if is_mostly_cjk(normalized) else LATIN_TARGET_CHARS

    sentences = split_sentences(normalized)
    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        if current:
            chunks.append(" ".join(current).strip())

    for sentence in sentences:
        if len(sentence) > target_chars:
            flush()
            # Carry-over does not apply across an over-long sentence — by
            # definition it already contains too much context.
            current = []
            current_len = 0
            chunks.extend(_split_long_sentence(sentence, target_chars))
            continue

        if current and current_len + len(sentence) + 1 > target_chars:
            flush()
            if overlap_sentences > 0:
                current = current[-overlap_sentences:]
                current_len = sum(len(s) + 1 for s in current)
            else:
                current = []
                current_len = 0

        current.append(sentence)
        current_len += len(sentence) + 1

    flush()
    return [c for c in chunks if c]


def get_settings() -> dict[str, Any]:
    """Load the single global LLM settings row with the API key decrypted."""
    with connect() as conn:
        return load_llm_settings(conn) or {}


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
        records: list[tuple[str, str]] = []
        for location, text in sections:
            for chunk in chunk_text(text):
                records.append((location, chunk))
        if not records:
            raise ValueError("No extractable text found.")

        logger.info(
            "ingest_chunked source_id=%s sections=%s chunks=%s",
            source_id,
            len(sections),
            len(records),
        )
        embeddings = await embed_texts([text for _, text in records], get_settings())
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
