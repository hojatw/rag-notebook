from tests.inspect_e5_chunk_tokens import chunk_category, is_table_like, percentile, summarize


def test_percentile_interpolates_without_extra_dependencies():
    assert percentile([10, 20, 30], 0.50) == 20
    assert percentile([10, 20, 30, 40], 0.95) == 38.5


def test_chunk_category_identifies_cjk_latin_and_mixed_text():
    assert chunk_category("這是一段繁體中文內容，描述部署設定與端點。") == "cjk"
    assert chunk_category("This is a plain English deployment note.") == "latin"
    assert chunk_category("Azure 服務 deployment endpoint 設定") == "mixed"


def test_table_like_heuristic_catches_dense_rows():
    table = "欄位|數值|比例\nA|12345|55%\nB|67890|45%\nC|11111|10%"
    assert is_table_like(table) is True
    assert chunk_category(table) == "table"


def test_summarize_empty_and_nonempty_values():
    assert summarize([])["count"] == 0
    stats = summarize([1, 2, 3])
    assert stats["count"] == 3
    assert stats["max"] == 3
    assert stats["mean"] == 2
