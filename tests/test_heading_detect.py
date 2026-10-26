"""A12 Phase 1：標題結構偵測（只進診斷，不改 chunk）。"""
from pathlib import Path

import pytest

from app import heading_detect, ingest
from app.heading_detect import (
    TIER_NONE,
    TIER_NUMBERING,
    TIER_OUTLINE,
    TIER_TOC_PAGE,
    detect_structure,
    numbering_depth,
)


@pytest.mark.parametrize(
    ("title", "depth"),
    [
        ("壹、計畫緣起", 1),
        ("一、會議目的", 2),
        ("（一）出席人員", 3),
        ("(二) 決議", 3),
        ("（1）細項", 5),
        ("第三章 研究方法", 1),
        ("第 2 節 樣本", 2),
        ("第 12 條", 3),
        ("第 6-1 條", 3),
        ("5.2.1 Treatment-Emergent Adverse Events", 3),
        ("5.2.1.1 Subgroups", 4),
        ("1. Introduction", 1),
        ("Chapter 4 Results", 1),
        ("本研究的限制如下", None),
        ("2024 年度報告", None),
    ],
)
def test_numbering_depth(title, depth):
    assert numbering_depth(title) == depth


def _docx(path: Path, paragraphs: list[tuple[str, str | None]]) -> Path:
    from docx import Document

    doc = Document()
    for text, style in paragraphs:
        doc.add_paragraph(text, style=style)
    doc.save(str(path))
    return path


def test_docx_heading_styles_are_the_outline_tier(tmp_path):
    path = _docx(tmp_path / "a.docx", [
        ("緒論", "Heading 1"), ("內文。", None),
        ("研究方法", "Heading 1"), ("樣本", "Heading 2"), ("內文。", None),
    ])
    result = detect_structure(path, ingest.extract_sections(path).sections)
    assert result.tier == TIER_OUTLINE
    assert [(h.title, h.depth) for h in result.headings] == [("緒論", 1), ("研究方法", 1), ("樣本", 2)]
    assert result.summary() == {"tier": TIER_OUTLINE, "headings": 3, "levels": 2, "toc_pages": []}


def test_outline_with_too_few_nodes_falls_through_to_numbering(tmp_path):
    # 一兩個節點的大綱不是可用的樹（Phase 0 實測常見），要往下一層找。
    path = _docx(tmp_path / "b.docx", [
        ("會議紀錄", "Heading 1"),
        ("一、會議目的", None), ("說明。", None),
        ("二、討論事項", None), ("（一）時程", None), ("說明。", None),
    ])
    result = detect_structure(path, ingest.extract_sections(path).sections)
    assert result.tier == TIER_NUMBERING
    assert result.summary()["headings"] == 3
    assert result.summary()["levels"] == 2


def test_html_h_tags_are_the_outline_tier(tmp_path):
    path = tmp_path / "a.html"
    path.write_text("<h1>A</h1><p>x</p><h2>B</h2><p>y</p><h2>C</h2>", encoding="utf-8")
    result = detect_structure(path, ingest.extract_sections(path).sections)
    assert result.tier == TIER_OUTLINE
    assert result.summary()["levels"] == 2


def test_toc_page_tier_merges_consecutive_pages_and_survives_a_broken_outline(tmp_path):
    # 不是真的 PDF：outline 那一層會出錯，必須自己吞掉並往下走。
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"not a pdf")
    toc_a = "\n".join(f"{i}. 第 {i} 部分 ........ {i * 3}" for i in range(1, 5))
    toc_b = "\n".join(f"{i}. 第 {i} 部分 ........ {i * 3}" for i in range(5, 9))
    sections = [
        ("page 2 paragraph 1", toc_a + "\n4.1 細項 ...... 13"),
        ("page 3 paragraph 1", toc_b),
        ("page 5 paragraph 1", "正文開始。"),
    ]
    result = detect_structure(path, sections)
    assert result.tier == TIER_TOC_PAGE
    assert result.toc_pages == [2, 3]
    assert len(result.headings) == 9


def test_running_header_is_not_counted_as_many_headings(tmp_path):
    sections = [(f"page {i} paragraph 1", "1 Introduction\n內文。") for i in range(1, 6)]
    assert detect_structure(tmp_path / "x.pdf", sections).tier == TIER_NONE


def test_plain_document_is_tier_none(tmp_path):
    path = _docx(tmp_path / "c.docx", [("沒有任何標題的一段話。", None)])
    assert detect_structure(path, ingest.extract_sections(path).sections).tier == TIER_NONE


def test_extraction_reports_structure_without_changing_sections_or_chunks(tmp_path, monkeypatch):
    """Phase 1 的硬性要求：偵測只進診斷，sections 與 chunk 必須和偵測前一模一樣。"""
    path = _docx(tmp_path / "d.docx", [
        ("緒論", "Heading 1"), ("第一段內文。" * 30, None),
        ("方法", "Heading 1"), ("第二段內文。" * 30, None),
        ("結果", "Heading 2"), ("第三段內文。" * 30, None),
    ])
    with_detection = ingest.extract_sections(path)
    assert with_detection.details["structure"]["tier"] == TIER_OUTLINE

    monkeypatch.setattr(ingest, "_STRUCTURE_SUFFIXES", set())
    without_detection = ingest.extract_sections(path)
    assert "structure" not in without_detection.details
    assert with_detection.sections == without_detection.sections
    assert ingest.chunk_sections(with_detection.sections) == ingest.chunk_sections(without_detection.sections)


def test_a_failing_detector_layer_never_fails_extraction(tmp_path, monkeypatch):
    def boom(_path):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(heading_detect, "_outline_headings", boom)
    path = _docx(tmp_path / "e.docx", [("一、目的", None), ("二、範圍", None), ("三、時程", None)])
    result = ingest.extract_sections(path)
    assert result.sections
    assert result.details["structure"]["tier"] == TIER_NUMBERING


def test_structure_round_trips_into_ingest_diagnostics(tmp_path):
    """producer→reader：diagnostics_json 的 details.structure 由真正的 extract_sections 產生。"""
    path = _docx(tmp_path / "f.docx", [("A", "Heading 1"), ("B", "Heading 2"), ("C", "Heading 2")])
    extraction = ingest.extract_sections(path)
    diagnostics = ingest.collect_ingest_diagnostics(extraction, ingest.chunk_sections(extraction.sections))
    assert diagnostics["details"]["structure"] == {
        "tier": TIER_OUTLINE, "headings": 3, "levels": 2, "toc_pages": [],
    }
