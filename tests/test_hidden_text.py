"""W5：隱藏文字通道。

讀者在原始檔看不到、但擷取會帶進 chunk 的文字（DOCX 的「隱藏」字型格式、HTML 的
`visibility:hidden`、圖片替代文字、PPTX 備忘稿與隱藏投影片）。這裡釘住兩件事：

1. 這些文字**仍然會被擷取**——刻意不丟棄（丟棄會傷召回，見 docs/SECURITY.md）。
2. 每個通道的字元數會進 `details.hidden_text`；讀者真的看不到的兩個通道另外產生
   使用者可見的注意事項。

測試素材只用無害的標記字串，不含攻擊內容。
"""
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.shared import Pt, RGBColor
from pptx import Presentation

from app import ingest
from app.i18n import t
from app.ingest import (
    HIDDEN_DOCX_VANISH,
    HIDDEN_HTML_ATTRIBUTE_TEXT,
    HIDDEN_HTML_META_DESCRIPTION,
    HIDDEN_HTML_VISIBILITY,
    HIDDEN_PPTX_HIDDEN_SLIDES,
    HIDDEN_PPTX_NOTES,
    NOTE_DOCX_HIDDEN_TEXT,
    NOTE_HTML_HIDDEN_TEXT,
    extract_sections,
)


def _all_text(extraction) -> str:
    return "\n".join(text for _, text in extraction.sections)


def _warning(extraction, code: str) -> dict | None:
    diagnostics = ingest.collect_ingest_diagnostics(extraction, ingest.chunk_sections(extraction.sections))
    return next((w for w in diagnostics["warnings"] if w["code"] == code), None)


# -------------------- DOCX --------------------

def test_docx_hidden_runs_are_extracted_counted_and_warned(tmp_path):
    doc = Document()
    paragraph = doc.add_paragraph("可見段落。")
    paragraph.add_run("HIDDEN_BODY").font.hidden = True
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].add_run("HIDDEN_CELL").font.hidden = True
    doc.sections[0].header.add_paragraph().add_run("HIDDEN_HEADER").font.hidden = True
    path = tmp_path / "hidden.docx"
    doc.save(str(path))

    extraction = extract_sections(path)

    text = _all_text(extraction)
    for marker in ("HIDDEN_BODY", "HIDDEN_CELL", "HIDDEN_HEADER"):
        assert marker in text  # 刻意不丟棄
    expected = len("HIDDEN_BODY") + len("HIDDEN_CELL") + len("HIDDEN_HEADER")
    assert extraction.details["hidden_text"] == {HIDDEN_DOCX_VANISH: expected}
    assert NOTE_DOCX_HIDDEN_TEXT in extraction.notes
    # producer→reader：注意事項的 payload 由真正的擷取結果產生，文案的佔位符吃得到它。
    warning = _warning(extraction, NOTE_DOCX_HIDDEN_TEXT)
    assert warning == {"code": NOTE_DOCX_HIDDEN_TEXT, "chars": expected}
    rendered = t("diag.warn_" + warning["code"], **warning)
    assert rendered != "diag.warn_" + warning["code"]
    assert str(expected) in rendered


def test_docx_hidden_via_style_counts_and_explicit_off_does_not(tmp_path):
    doc = Document()
    char_style = doc.styles.add_style("SecretChar", WD_STYLE_TYPE.CHARACTER)
    char_style.font.hidden = True
    derived = doc.styles.add_style("SecretCharChild", WD_STYLE_TYPE.CHARACTER)
    derived.base_style = char_style
    para_style = doc.styles.add_style("SecretPara", WD_STYLE_TYPE.PARAGRAPH)
    para_style.font.hidden = True

    doc.add_paragraph().add_run("BY_CHAR_STYLE", style="SecretChar")
    doc.add_paragraph().add_run("BY_BASED_ON", style="SecretCharChild")
    doc.add_paragraph("BY_PARA_STYLE", style="SecretPara")
    # 樣式說隱藏，但這個 run 明確關掉：Word 會顯示它，所以不能算。
    shown = doc.add_paragraph(style="SecretPara").add_run("SHOWN_AGAIN")
    shown.font.hidden = False
    path = tmp_path / "styled.docx"
    doc.save(str(path))

    extraction = extract_sections(path)

    expected = len("BY_CHAR_STYLE") + len("BY_BASED_ON") + len("BY_PARA_STYLE")
    assert extraction.details["hidden_text"] == {HIDDEN_DOCX_VANISH: expected}
    assert "SHOWN_AGAIN" in _all_text(extraction)


def test_docx_white_and_tiny_text_is_not_flagged(tmp_path):
    """刻意不做：白字／極小字靠排版判斷，誤報多（深色表格裡的白字是合法排版）。"""
    doc = Document()
    paragraph = doc.add_paragraph("可見段落。")
    paragraph.add_run("WHITE_TEXT").font.color.rgb = RGBColor(255, 255, 255)
    paragraph.add_run("TINY_TEXT").font.size = Pt(1)
    path = tmp_path / "plain.docx"
    doc.save(str(path))

    extraction = extract_sections(path)

    assert "WHITE_TEXT" in _all_text(extraction)
    assert "hidden_text" not in extraction.details
    assert NOTE_DOCX_HIDDEN_TEXT not in extraction.notes


# -------------------- HTML --------------------

def test_html_visibility_hidden_is_extracted_counted_and_warned(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        """<html><body>
        <p>VISIBLE_CONTENT</p>
        <div style="visibility: hidden">OUTER_HIDDEN<span style="visibility:hidden">INNER_HIDDEN</span></div>
        <p style="color: red">STYLED_BUT_VISIBLE</p>
        </body></html>""",
        encoding="utf-8",
    )

    extraction = extract_sections(path)

    text = _all_text(extraction)
    assert "OUTER_HIDDEN" in text and "INNER_HIDDEN" in text  # 刻意不丟棄
    # 巢狀的隱藏元素只算一次。
    expected = len("OUTER_HIDDEN") + len("INNER_HIDDEN")
    assert extraction.details["hidden_text"] == {HIDDEN_HTML_VISIBILITY: expected}
    assert NOTE_HTML_HIDDEN_TEXT in extraction.notes
    warning = _warning(extraction, NOTE_HTML_HIDDEN_TEXT)
    assert warning == {"code": NOTE_HTML_HIDDEN_TEXT, "chars": expected}
    rendered = t("diag.warn_" + warning["code"], **warning)
    assert rendered != "diag.warn_" + warning["code"]
    assert str(expected) in rendered


def test_html_attribute_and_meta_text_is_counted_without_a_warning(tmp_path):
    """alt／title／meta 很常見，而且擷取時已有 `[image: …]` 標示：只記字數，不提示。"""
    path = tmp_path / "page.html"
    path.write_text(
        """<html><head><meta name="description" content="META_DESC">
        <meta property="og:description" content="OG_DESC"></head><body>
        <p>VISIBLE_CONTENT</p>
        <img src="x.png" alt="ALT_TEXT"><a href="/s" title="TITLE_TEXT">spec</a>
        <input type="text" value="INPUT_VALUE">
        </body></html>""",
        encoding="utf-8",
    )

    extraction = extract_sections(path)

    assert extraction.details["hidden_text"] == {
        HIDDEN_HTML_ATTRIBUTE_TEXT: len("ALT_TEXT") + len("TITLE_TEXT") + len("INPUT_VALUE"),
        HIDDEN_HTML_META_DESCRIPTION: len("META_DESC") + len("OG_DESC"),
    }
    assert extraction.notes == []
    diagnostics = ingest.collect_ingest_diagnostics(extraction, ingest.chunk_sections(extraction.sections))
    assert not [w for w in diagnostics["warnings"] if "hidden" in w["code"]]


def test_html_without_hidden_channels_reports_nothing(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><body><p>VISIBLE_CONTENT</p></body></html>", encoding="utf-8")

    extraction = extract_sections(path)

    assert "hidden_text" not in extraction.details


# -------------------- PPTX --------------------

def test_pptx_notes_and_hidden_slides_are_counted_without_a_warning(tmp_path):
    deck = Presentation()
    layout = deck.slide_layouts[1]
    shown = deck.slides.add_slide(layout)
    shown.shapes.title.text = "SHOWN_TITLE"
    shown.notes_slide.notes_text_frame.text = "NOTES_TEXT"
    hidden = deck.slides.add_slide(layout)
    hidden.shapes.title.text = "HIDDEN_SLIDE_TITLE"
    hidden._element.set("show", "0")
    path = tmp_path / "deck.pptx"
    deck.save(str(path))

    extraction = extract_sections(path)

    text = _all_text(extraction)
    assert "NOTES_TEXT" in text and "HIDDEN_SLIDE_TITLE" in text  # 刻意不丟棄
    assert extraction.details["hidden_text"] == {
        HIDDEN_PPTX_NOTES: len("NOTES_TEXT"),
        HIDDEN_PPTX_HIDDEN_SLIDES: len("HIDDEN_SLIDE_TITLE"),
    }
    assert extraction.notes == []


def test_pptx_without_notes_or_hidden_slides_reports_nothing(tmp_path):
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "SHOWN_TITLE"
    path = tmp_path / "deck.pptx"
    deck.save(str(path))

    assert "hidden_text" not in extract_sections(path).details


# -------------------- PDF --------------------

def _white_text_pdf(visible: str, white: str) -> bytes:
    """最小 PDF：一行黑字、一行白字（`1 1 1 rg`）。"""
    stream = (
        f"BT /F1 12 Tf 0 0 0 rg 1 0 0 1 56 760 Tm ({visible}) Tj "
        f"1 1 1 rg 1 0 0 1 56 740 Tm ({white}) Tj ET"
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_pdf_white_text_is_extracted_and_not_flagged(tmp_path):
    """現況（已知缺口）：PDF 白字會進 chunk，而且沒有任何診斷。

    偵測要逐字讀填色，成本高、誤報多，刻意不做（docs/SECURITY.md）。哪天補上偵測，
    這個測試會變紅，提醒同步改文件。
    """
    path = tmp_path / "white.pdf"
    path.write_bytes(_white_text_pdf("Visible policy line.", "WHITE_PDF_TEXT"))

    extraction = extract_sections(path)

    text = _all_text(extraction)
    assert "Visible policy line." in text
    assert "WHITE_PDF_TEXT" in text
    assert "hidden_text" not in extraction.details


# -------------------- 顯示 --------------------

def test_every_hidden_channel_has_display_copy():
    """預覽面板用 `diag.hidden_channel_<key>` 顯示；少一個鍵會直接顯示原始鍵名。"""
    for channel in ingest.HIDDEN_TEXT_CHANNELS:
        key = "diag.hidden_channel_" + channel
        assert t(key, chars=1) != key

