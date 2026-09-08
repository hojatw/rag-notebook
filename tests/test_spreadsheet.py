"""A6c spreadsheet ingestion — Q&A detection, generic-record fallback, token
budgeting, CSV encoding, and the diagnostics contract.

Design notes: docs/SPREADSHEET_INGESTION.md. Fixtures are built programmatically
so the suite carries no binary assets.
"""
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.ingest import extract_sections


def _write_xlsx(tmp_path: Path, sheets: dict[str, list[list]], hidden: set[str] = frozenset()) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
        if name in hidden:
            ws.sheet_state = "hidden"
    path = tmp_path / "fixture.xlsx"
    wb.save(str(path))
    return path


# -------------------- Q&A sheets --------------------

def test_qa_sheet_becomes_one_chunk_per_row(tmp_path):
    path = _write_xlsx(tmp_path, {"FAQ": [
        ["分類", "問題", "答案"],
        ["帳號登入", "忘記密碼怎麼辦？", "請在登入頁點選「忘記密碼」，依信件指示重設。"],
        ["帳號登入", "如何變更 email？", "到帳號設定頁修改，需要重新驗證。"],
    ]})

    result = extract_sections(path)

    assert result.extractor == "xlsx"
    assert result.pre_chunked is True          # rows must not be re-packed
    assert len(result.sections) == 2
    location, text = result.sections[0]
    assert location == 'sheet "FAQ" row 2'
    assert "Question:\n忘記密碼怎麼辦？" in text
    assert "Answer:\n請在登入頁" in text
    assert "Category" not in text               # trimmed preamble uses the sheet's own label
    assert "分類: 帳號登入" in text
    assert result.details["sheets"][0]["type"] == "qa_pairs"
    assert result.details["sheets"][0]["header"] == "detected"


def test_qa_detection_accepts_house_vocabulary(tmp_path):
    """Customer sheets rarely say 'question' — 客戶提問 must still resolve."""
    path = _write_xlsx(tmp_path, {"知識庫": [
        ["客戶提問", "回覆內容"],
        ["出貨要多久？", "一般為 3 個工作天。"],
    ]})

    result = extract_sections(path)

    assert result.details["sheets"][0]["type"] == "qa_pairs"
    assert "Question:\n出貨要多久？" in result.sections[0][1]


def test_headerless_two_column_sheet_is_treated_as_qa_but_flagged(tmp_path):
    path = _write_xlsx(tmp_path, {"Sheet1": [
        ["忘記密碼怎麼辦？", "點選忘記密碼。"],
        ["如何登出？", "點右上角登出。"],
    ]})

    result = extract_sections(path)

    sheet = result.details["sheets"][0]
    assert sheet["type"] == "qa_pairs"
    assert sheet["header"] == "generated"
    assert sheet["qa_columns"] == "auto_detected"   # the guess is recorded, not hidden
    assert len(result.sections) == 2
    assert result.sections[0][0] == 'sheet "Sheet1" row 1'


# -------------------- generic records --------------------

def test_non_qa_sheet_falls_back_to_records_and_says_so(tmp_path):
    path = _write_xlsx(tmp_path, {"客戶資料": [
        ["客戶ID", "公司名稱", "產業", "區域"],
        ["C001", "台灣大成製造", "鋼鐵", "台中"],
        ["C002", "南方電子", "電子", "高雄"],
    ]})

    result = extract_sections(path)

    assert result.details["sheets"][0]["type"] == "records"
    assert "spreadsheet_generic_records" in result.notes   # MVP limit stated, not implied
    text = result.sections[0][1]
    assert "Sheet: 客戶資料 · Columns: 客戶ID, 公司名稱, 產業, 區域" in text
    assert "客戶ID = C001" in text
    assert "公司名稱 = 台灣大成製造" in text


def test_record_rows_pack_until_the_token_budget(tmp_path, monkeypatch):
    """Packing is a vector-count economy; the budget, not a fixed count, ends a chunk."""
    import app.config as app_config
    monkeypatch.setattr(app_config.config.spreadsheet, "embed_token_budget", 60)
    rows = [["R%03d" % i, "描述文字內容" * 4] for i in range(12)]
    path = _write_xlsx(tmp_path, {"資料": [["編號", "說明"], *rows]})

    result = extract_sections(path)

    assert len(result.sections) > 1               # did not cram everything into one chunk
    assert all("rows" in loc or "row" in loc for loc, _ in result.sections)
    # Row ranges must be contiguous and complete: no row may be dropped.
    covered = " ".join(loc for loc, _ in result.sections)
    assert "row 2" in covered and "13" in covered


def test_single_over_budget_row_is_split_repeating_the_identifier(tmp_path, monkeypatch):
    """A wide row must never be embedded whole and silently truncated."""
    import app.config as app_config
    monkeypatch.setattr(app_config.config.spreadsheet, "embed_token_budget", 80)
    columns = ["客戶ID"] + [f"欄位{i}" for i in range(1, 16)]
    values = ["C001"] + ["這是一段相當長的欄位內容用來撐爆預算" for _ in range(15)]
    path = _write_xlsx(tmp_path, {"寬表": [columns, values]})

    result = extract_sections(path)

    assert len(result.sections) > 1
    assert all("part" in location for location, _ in result.sections)
    # Every part identifies its record, otherwise a retrieved fragment is orphaned.
    assert all("客戶ID = C001" in text for _, text in result.sections)


# -------------------- hidden sheets, caps, formulas --------------------

def test_hidden_sheets_are_skipped_and_recorded(tmp_path):
    path = _write_xlsx(
        tmp_path,
        {"公開": [["問題", "答案"], ["A?", "B"]], "暫存": [["問題", "答案"], ["X?", "Y"]]},
        hidden={"暫存"},
    )

    result = extract_sections(path)

    assert result.details["skipped_sheets"] == ["暫存"]
    assert "spreadsheet_hidden_sheets_skipped" in result.notes
    assert all('sheet "暫存"' not in location for location, _ in result.sections)


def test_oversized_file_is_refused_with_a_clear_error(tmp_path, monkeypatch):
    """A zip container's on-disk size says nothing about what it expands to."""
    import app.config as app_config
    monkeypatch.setattr(app_config.config.runtime, "extract_max_file_bytes", 10)
    path = _write_xlsx(tmp_path, {"FAQ": [["問題", "答案"], ["a", "b"]]})

    with pytest.raises(ValueError, match="too large"):
        extract_sections(path)


def test_oversized_csv_is_refused_before_being_read_into_memory(tmp_path, monkeypatch):
    import app.config as app_config
    monkeypatch.setattr(app_config.config.runtime, "extract_max_file_bytes", 10)
    path = tmp_path / "big.csv"
    path.write_text("問題,答案\n" + "a,b\n" * 100, encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        extract_sections(path)


# -------------------- CSV --------------------

def test_csv_utf8_is_read_with_detected_delimiter(tmp_path):
    path = tmp_path / "faq.csv"
    path.write_text("問題,答案\n如何登入？,用帳號密碼登入。\n", encoding="utf-8")

    result = extract_sections(path)

    assert result.extractor == "csv"
    assert result.details["encoding"]["encoding"] == "utf-8"
    assert result.details["delimiter"] == ","
    assert result.details["sheets"][0]["type"] == "qa_pairs"
    assert result.sections[0][0] == 'sheet "faq" row 2'


def test_csv_utf16_bom_streams_with_csv_newline_semantics(tmp_path):
    path = tmp_path / "utf16.csv"
    path.write_text("問題,答案\n如何登入？,使用帳密。\n", encoding="utf-16")

    result = extract_sections(path)

    assert result.details["encoding"] == {"encoding": "utf-16", "source": "bom"}
    assert result.details["delimiter"] == ","
    assert len(result.sections) == 1
    assert "如何登入？" in result.sections[0][1]


def test_csv_big5_is_decoded_and_flagged(tmp_path):
    """Big5/CP950 exports are common in zh-TW and must not be silently mojibake'd."""
    path = tmp_path / "big5.csv"
    path.write_bytes("問題,答案\n如何退貨？,七天內可退貨。\n".encode("cp950"))

    result = extract_sections(path)

    assert result.details["encoding"]["source"] == "detected"
    assert "csv_encoding_fallback" in result.notes
    assert "如何退貨？" in result.sections[0][1]     # decoded, not replacement chars


def test_csv_gbk_is_decoded_not_mistaken_for_korean(tmp_path):
    """GBK/GB18030 exports (zh-CN) must decode as Chinese, not cp949 mojibake.

    charset-normalizer 3.4.9 scored this sample as cp949 (Korean) and returned
    "櫓벌盧땡繫斤" — plausible-looking text, so nothing raised and the garbage
    would have been chunked and embedded as though it were the document. 3.5.1
    picks gb18030 correctly. Pinned because the failure mode is silent: a
    customer sending a Simplified Chinese CSV would get an indexed source full
    of Korean-looking noise and no error anywhere to explain it.
    """
    path = tmp_path / "gbk.csv"
    path.write_bytes("问题,答案\n如何退货？,七天内可退货。\n".encode("gbk"))

    result = extract_sections(path)

    assert result.details["encoding"]["source"] == "detected"
    assert "如何退货？" in result.sections[0][1]
    assert "櫓" not in result.sections[0][1]


def test_csv_streams_and_recovers_when_non_utf8_bytes_start_after_the_sample(
    tmp_path, monkeypatch
):
    """A valid ASCII head must not hide Big5 bytes that appear after 64 KiB."""
    import app.config as app_config

    monkeypatch.setattr(app_config.config.spreadsheet, "max_rows", 2)
    monkeypatch.setattr(app_config.config.spreadsheet, "max_cols", 2)
    path = tmp_path / "late-big5.csv"
    raw = (
        b"q,a,ignored\n"
        + b"padding?,\""
        + b"a" * 70_000
        + b"\",drop-column\n"
        + "如何退貨？,七天內可退貨。,drop-column\n".encode("cp950")
        + "第三題？,第三答,drop-column\n".encode("cp950")
    )
    path.write_bytes(raw)

    def _read_bytes_must_not_be_used(_path):
        raise AssertionError("CSV ingestion must stream instead of calling Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", _read_bytes_must_not_be_used)
    result = extract_sections(path)

    assert result.details["encoding"]["source"] == "detected"
    # Match on content, not position: this fixture's 70 KB padding cell is itself
    # over the token budget and now splits into several parts, so the second Q&A
    # row is no longer at a fixed index. What matters here is that its Big5 bytes
    # were decoded at all.
    assert any("如何退貨？" in text for _, text in result.sections)
    assert all("第三題？" not in text for _, text in result.sections)
    assert all("drop-column" not in text for _, text in result.sections)
    assert result.details["truncated_sheets"] == ["late-big5"]
    assert "spreadsheet_truncated" in result.notes


def test_csv_preserves_newlines_inside_quoted_fields(tmp_path):
    path = tmp_path / "quoted-newline.csv"
    path.write_text(
        '問題,答案\n"如何操作？","第一行\n第二行"\n',
        encoding="utf-8",
    )

    result = extract_sections(path)

    assert len(result.sections) == 1
    assert "Answer:\n第一行\n第二行" in result.sections[0][1]


def _nonspace(text: str) -> str:
    return "".join(text.split())


def test_long_qa_answer_is_split_into_parts_that_repeat_the_question(tmp_path, monkeypatch):
    """A Q&A row was emitted whole at any length, so one long answer killed the file.

    Q&A sheets in the wild paste whole regulations into the answer cell; ~660
    Chinese characters is all it takes to pass a 512-token window, and vLLM
    refuses an over-long input rather than truncating it, so the failure is the
    entire source, not one row. Each part has to repeat the question or the tail
    pieces stop being reachable by the question they answer.
    """
    import app.config as app_config

    monkeypatch.setattr(app_config.config.spreadsheet, "embed_token_budget", 120)
    answer = "本條規定之內容依主管機關公告辦理，並自公告日起生效。" * 12
    path = _write_xlsx(tmp_path, {"FAQ": [["編號", "客戶提問", "回覆內容"],
                                          ["1", "請問核決權限的規定為何？", answer]]})

    result = extract_sections(path)

    assert len(result.sections) > 1, "the long answer was not split at all"
    assert all("part" in location for location, _ in result.sections)
    assert result.sections[0][0].endswith(f"part 1/{len(result.sections)}")
    # Every part stays reachable by the question, and names its sheet row.
    assert all("請問核決權限的規定為何？" in text for _, text in result.sections)
    assert all('sheet "FAQ" row 2' in location for location, _ in result.sections)
    # Nothing is dropped: splitting must not become silent truncation.
    answers = [text.split("Answer:\n", 1)[1] for _, text in result.sections]
    assert _nonspace("".join(answers)) == _nonspace(answer)


def test_short_qa_answer_still_makes_exactly_one_chunk(tmp_path):
    """The row remains the unit whenever it fits — splitting is the exception."""
    path = _write_xlsx(tmp_path, {"FAQ": [["編號", "客戶提問", "回覆內容"],
                                          ["1", "請購單多久核准？", "採購部收件後三個工作日內完成初審。"]]})

    result = extract_sections(path)

    assert len(result.sections) == 1
    assert result.sections[0][0] == 'sheet "FAQ" row 2'
    assert "part" not in result.sections[0][0]


def test_single_over_budget_cell_is_split_repeating_its_column_name(tmp_path, monkeypatch):
    """One oversized cell escaped the packing loop entirely.

    `_split_wide_row` only closed a group when `current` was non-empty, so a cell
    bigger than the whole budget landed in an empty group and was emitted at full
    length — the wide-row splitter looking like it had done its job. A contract
    clause or a pasted procedure in a free-text column reaches that size easily.
    """
    import app.config as app_config

    monkeypatch.setattr(app_config.config.spreadsheet, "embed_token_budget", 120)
    clause = "本條款之內容包含各項權利義務之詳細說明，並依相關法令辦理。" * 12
    path = _write_xlsx(tmp_path, {"條款": [["條號", "內容"], ["第一條", clause]]})

    result = extract_sections(path)

    assert len(result.sections) > 1
    assert all("part" in location for location, _ in result.sections)
    # A tail piece must still say which field it came from.
    assert all("內容 = " in text for _, text in result.sections)
    assert all("條號 = 第一條" in text for _, text in result.sections)
    bodies = [text.split("內容 = ", 1)[1] for _, text in result.sections]
    assert _nonspace("".join(bodies)) == _nonspace(clause)


def test_oversized_identifier_column_is_split_not_repeated(tmp_path, monkeypatch):
    """Column 0 is repeated on every part, so an oversized one poisons all of them.

    No amount of splitting the other columns helps when the identifier alone is
    over budget. It gets demoted to an ordinary column and split like any other —
    dropping it instead would be silent, and column 0 is usually the record name.
    """
    import app.config as app_config

    monkeypatch.setattr(app_config.config.spreadsheet, "embed_token_budget", 120)
    essay = "這一欄被填進了整段說明文字而不是識別碼，長度遠超過一個識別碼該有的樣子。" * 10
    path = _write_xlsx(tmp_path, {"表": [["說明", "數值"], [essay, "42"]]})

    result = extract_sections(path)

    assert len(result.sections) > 1
    assert all("part" in location for location, _ in result.sections)
    # Demoted, not truncated: the whole cell is still present across the parts.
    recovered = "".join(
        _nonspace(text.split("說明 = ", 1)[1]) for _, text in result.sections if "說明 = " in text
    )
    assert recovered == _nonspace(essay)
