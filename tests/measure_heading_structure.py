"""A12 Phase 0：量測文件結構訊號覆蓋率與 chunk 跨章節比例 — 一次性、離線、唯讀。

回答 docs/ROADMAP.md A12 Phase 0 的兩個問題：

(a) 多少比例的文件帶可用結構訊號，依 outline > TOC 頁 > 編號樣式 > 字級
    的順序分層（每份文件只算最高的那一層；字級只當加分訊號另列）。
(b) 用 `app/ingest.py` 現有的 `extract_sections` + `chunk_sections`（只呼叫、
    不修改）切出的 chunk，有多少比例跨越章節邊界。

不碰網路、資料庫、向量庫，也不引入新依賴（pypdf / pdfplumber / python-docx
都是既有依賴）。用法：

    PYTHONPATH=. .venv/bin/python -m tests.measure_heading_structure DIR_OR_FILE ...

「跨越」的判定：chunk 內某個標題出現在前面已有 >= STRADDLE_MIN_PREFIX 個
非空白字元之後，代表這個 chunk 同時含有上一節的內文與下一節的開頭。只在
有標題可比對的文件上計算；`none` 層的文件不列入 (b) 的分母。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import chunk_sections, extract_sections  # noqa: E402

MIN_OUTLINE_NODES = 3  # 2026-08-22 量到「有 outline 但只有 1 個節點」的情況
MIN_TOC_LINES = 5
MIN_NUMBERED_HEADINGS = 3
MAX_HEADING_CHARS = 40
STRADDLE_MIN_PREFIX = 30
TOC_CHUNK_HITS = 3
FONT_SAMPLE_PAGES = 20

_CN_NUM = "一二三四五六七八九十百"
_NUMBERED_HEADING_RE = re.compile(
    r"^\s*("
    rf"第[{_CN_NUM}\d]+[章節篇部]"
    r"|[壹貳參叁肆伍陸柒捌玖拾]+[、．.]"
    rf"|[{_CN_NUM}]+[、．]"
    rf"|[（(][{_CN_NUM}]+[）)]"
    r"|\d{1,2}(\.\d{1,2}){0,3}\.?\s+\S"
    r"|(Chapter|Section)\s+\d+"
    r")"
)
_TOC_LINE_RE = re.compile(r"^(?P<title>.{2,80}?)\s*(?:[.．·…_\-\s]{3,})\s*(?P<page>\d{1,4})\s*$")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub("", text)


def _flatten_outline(items, out: list[str]) -> None:
    for item in items:
        if isinstance(item, list):
            _flatten_outline(item, out)
        else:
            title = getattr(item, "title", None)
            if title and title.strip():
                out.append(title.strip())


def outline_titles(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        try:
            titles: list[str] = []
            _flatten_outline(PdfReader(str(path)).outline, titles)
            return titles
        except Exception:
            return []
    if path.suffix.lower() == ".docx":
        # DOCX 的對應物是 Heading / 標題 樣式，與 PDF outline 同屬作者明示的結構。
        from docx import Document

        titles = []
        for para in Document(str(path)).paragraphs:
            style = (para.style.name or "") if para.style is not None else ""
            if (style.startswith("Heading") or style.startswith("標題")) and para.text.strip():
                titles.append(para.text.strip())
        return titles
    return []


def _lines(sections: list[tuple[str, str]]):
    for location, text in sections:
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield location, line


def toc_titles(sections: list[tuple[str, str]]) -> list[str]:
    """同一頁上 >= MIN_TOC_LINES 行「標題 + 點引線/空白 + 頁碼」且頁碼大致遞增。"""
    by_page: dict[str, list[tuple[str, int]]] = {}
    for location, line in _lines(sections):
        match = _TOC_LINE_RE.match(line)
        if match and not match.group("title").strip().isdigit():
            page = location.split(" paragraph")[0]
            by_page.setdefault(page, []).append((match.group("title").strip(), int(match.group("page"))))
    titles: list[str] = []
    for entries in by_page.values():
        if len(entries) < MIN_TOC_LINES:
            continue
        pages = [p for _, p in entries]
        ascending = sum(1 for a, b in zip(pages, pages[1:]) if b >= a)
        if ascending >= 0.8 * (len(pages) - 1):
            titles.extend(t for t, _ in entries)
    return titles


def numbered_titles(sections: list[tuple[str, str]]) -> list[str]:
    titles = []
    for _, line in _lines(sections):
        if len(line) <= MAX_HEADING_CHARS and _NUMBERED_HEADING_RE.match(line) and not _TOC_LINE_RE.match(line):
            titles.append(line)
    return titles


def large_font_share(path: Path) -> float | None:
    """字級大於內文眾數 15% 的字元比例；只當加分訊號。"""
    if path.suffix.lower() != ".pdf":
        return None
    import pdfplumber

    sizes: Counter[float] = Counter()
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:FONT_SAMPLE_PAGES]:
                for char in page.chars:
                    if char.get("text", "").strip():
                        sizes[round(char["size"], 1)] += 1
    except Exception:
        return None
    if not sizes:
        return None
    body = sizes.most_common(1)[0][0]
    big = sum(n for size, n in sizes.items() if size > body * 1.15)
    return big / sum(sizes.values())


def straddling_chunks(chunks: list[tuple[str, str]], titles: list[str]) -> tuple[int, int]:
    """回傳 (跨章節 chunk 數, 目錄型 chunk 數)。

    含 >= TOC_CHUNK_HITS 個標題的 chunk 視為目錄頁本身（Phase 1 會標記不索引），
    不算跨章節，另列。
    """
    keys = {_norm(t) for t in titles if len(_norm(t)) >= 4}
    straddle = toc_like = 0
    for _, text in chunks:
        flat = _norm(text)
        hits = [flat.find(k) for k in keys]
        if sum(1 for h in hits if h >= 0) >= TOC_CHUNK_HITS:
            toc_like += 1
        elif any(h >= STRADDLE_MIN_PREFIX for h in hits):
            straddle += 1
    return straddle, toc_like


def measure(path: Path) -> dict:
    result = extract_sections(path)
    sections = result.sections
    tier, titles = "none", []
    outline = outline_titles(path)
    if len(outline) >= MIN_OUTLINE_NODES:
        tier, titles = "outline", outline
    elif len(toc := toc_titles(sections)) >= MIN_TOC_LINES:
        tier, titles = "toc_page", toc
    elif len(numbered := numbered_titles(sections)) >= MIN_NUMBERED_HEADINGS:
        tier, titles = "numbering", numbered
    chunks = chunk_sections(sections)
    straddle, toc_like = straddling_chunks(chunks, titles) if titles else (0, 0)
    return {
        "file": path.name,
        "tier": tier,
        "outline_nodes": len(outline),
        "headings": len(titles),
        "font_share": large_font_share(path),
        "chunks": len(chunks),
        "straddle": straddle,
        "toc_like": toc_like,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    files: list[Path] = []
    for p in args.paths:
        files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in {".pdf", ".docx"}) if p.is_dir() else [p])
    rows, seen = [], set()
    for f in files:
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        if digest in seen:  # 同一份檔案上傳到多個 notebook 只算一次
            continue
        seen.add(digest)
        try:
            rows.append(measure(f))
        except Exception as exc:  # 一份壞檔不該中斷整次量測
            print(f"SKIP {f.name}: {exc}", file=sys.stderr)
    print("| file | tier | outline nodes | headings | big-font % | chunks | TOC-like | straddle |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        fs = "-" if r["font_share"] is None else f"{r['font_share']:.1%}"
        print(f"| {r['file'][-40:]} | {r['tier']} | {r['outline_nodes']} | {r['headings']} | {fs} | {r['chunks']} | {r['toc_like']} | {r['straddle']} |")
    tiers = Counter(r["tier"] for r in rows)
    print(f"\n(a) n={len(rows)}: " + ", ".join(f"{t} {tiers[t]} ({tiers[t] / len(rows):.0%})" for t in ("outline", "toc_page", "numbering", "none")))
    structured = [r for r in rows if r["tier"] != "none"]
    total = sum(r["chunks"] - r["toc_like"] for r in structured)
    straddle = sum(r["straddle"] for r in structured)
    if total:
        print(f"(b) 有結構的 {len(structured)} 份文件：{straddle}/{total} chunks 跨章節（已排除目錄型 chunk）({straddle / total:.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
