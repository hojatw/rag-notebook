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
    """Extract text sections from a supported source file."""
    suffix = path.suffix.lower()
    logger.info("extract_started path=%s suffix=%s", path.name, suffix)
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        sections = [
            (f"page {index}", page.extract_text() or "")
            for index, page in enumerate(reader.pages, start=1)
        ]
        logger.info("extract_completed path=%s sections=%s", path.name, len(sections))
        return sections
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
        logger.info("extract_completed path=%s sections=1", path.name)
        return [("document", text)]
    if suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        for element in soup(["script", "style"]):
            element.decompose()
        logger.info("extract_completed path=%s sections=1", path.name)
        return [("html", soup.get_text("\n"))]
    logger.info("extract_completed path=%s sections=1", path.name)
    return [("document", path.read_text(encoding="utf-8", errors="ignore"))]


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 160) -> list[str]:
    """Split normalized text into overlapping chunks for embedding."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + max_chars, len(normalized))
        split_at = normalized.rfind(". ", start, end)
        if split_at > start + max_chars // 2:
            end = split_at + 1
        chunks.append(normalized[start:end].strip())
        if end == len(normalized):
            break
        start = max(0, end - overlap)
    return chunks


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
