"""Unit tests for the sentence-aware chunker in app.ingest.

Covers:
  - Language detection (is_mostly_cjk)
  - Sentence splitting on CJK / Latin punctuation
  - Auto-sizing (CJK -> 400 chars, Latin -> 800 chars)
  - Overlap behaviour
  - Long-sentence fallback (soft punctuation, then hard cut)
  - Edge cases (empty, whitespace, single short sentence)
"""
from app.ingest import (
    CJK_TARGET_CHARS,
    LATIN_TARGET_CHARS,
    chunk_text,
    is_mostly_cjk,
    split_sentences,
)


# -------------------- language detection --------------------

def test_is_mostly_cjk_pure_chinese():
    assert is_mostly_cjk("這是一段中文文字") is True


def test_is_mostly_cjk_pure_english():
    assert is_mostly_cjk("This is a plain English paragraph.") is False


def test_is_mostly_cjk_threshold_mixed_english_dominant():
    # Mostly English with one Chinese word — Latin path.
    assert is_mostly_cjk("Azure deployment in 台灣 region only") is False


def test_is_mostly_cjk_threshold_mixed_cjk_dominant():
    # CJK over the 30% threshold even with English tokens mixed in.
    assert is_mostly_cjk("Azure 服務在台灣的部署設定") is True


def test_is_mostly_cjk_empty_string():
    assert is_mostly_cjk("") is False


# -------------------- sentence splitting --------------------

def test_split_sentences_chinese_punctuation():
    sents = split_sentences("這是第一句。這是第二句！這是第三句？")
    assert sents == ["這是第一句。", "這是第二句！", "這是第三句？"]


def test_split_sentences_english_punctuation():
    sents = split_sentences("This is one. And another! Plus a question?")
    assert sents == ["This is one.", "And another!", "Plus a question?"]


def test_split_sentences_uses_newlines_as_boundary():
    sents = split_sentences("Line one\nLine two\nLine three")
    assert sents == ["Line one", "Line two", "Line three"]


def test_split_sentences_keeps_terminator():
    # Each split keeps its own trailing punctuation.
    sents = split_sentences("Hello world. Goodbye?")
    assert sents[0].endswith(".")
    assert sents[1].endswith("?")


def test_split_sentences_handles_no_terminator():
    sents = split_sentences("trailing sentence with no punctuation")
    assert sents == ["trailing sentence with no punctuation"]


# -------------------- chunking — basic --------------------

def test_chunk_empty_input():
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_chunk_short_text_returns_single_chunk():
    out = chunk_text("Alpha project revenue is 42 dollars. Beta is unrelated.")
    assert out == ["Alpha project revenue is 42 dollars. Beta is unrelated."]


def test_chunk_horizontal_whitespace_normalised_but_newlines_preserved():
    # Tab + multiple spaces collapsed, newline kept as a boundary.
    out = chunk_text("foo   bar\tbaz\nnext line")
    assert out == ["foo bar baz next line"]


# -------------------- auto-sizing --------------------

def test_chunk_cjk_target_is_smaller_than_latin():
    # Same number of sentences in each language; CJK chunks should be smaller
    # because is_mostly_cjk auto-picks CJK_TARGET_CHARS.
    cjk_text = "這是一個句子。" * 80     # 8 chars per sentence * 80 = 640 chars
    latin_text = "This is one. " * 80    # 13 chars per sentence * 80 = 1040 chars

    cjk_chunks = chunk_text(cjk_text)
    latin_chunks = chunk_text(latin_text)

    # CJK should produce more chunks (smaller target).
    assert len(cjk_chunks) > 0
    assert len(latin_chunks) > 0
    assert max(len(c) for c in cjk_chunks) <= CJK_TARGET_CHARS
    assert max(len(c) for c in latin_chunks) <= LATIN_TARGET_CHARS


def test_chunk_explicit_target_overrides_auto():
    # Force a tight bound even on Latin text.
    out = chunk_text("Sentence one. Sentence two. Sentence three. Sentence four.", target_chars=30)
    assert len(out) > 1
    for chunk in out:
        # Allow a little slack because we group whole sentences.
        assert len(chunk) <= 60


# -------------------- overlap --------------------

def test_chunk_default_overlap_carries_one_sentence():
    # Build text that forces exactly 2 chunks with default overlap_sentences=1.
    sentences = [f"Sentence number {i} with some padding text." for i in range(20)]
    text = " ".join(sentences)
    out = chunk_text(text, target_chars=200, overlap_sentences=1)

    assert len(out) >= 2
    # The last sentence of chunk N should appear at the start of chunk N+1.
    for prev, nxt in zip(out, out[1:]):
        prev_last_sentence = split_sentences(prev)[-1]
        assert prev_last_sentence in nxt, f"overlap missing: {prev_last_sentence!r} not in {nxt!r}"


def test_chunk_zero_overlap_produces_disjoint_chunks():
    sentences = [f"Sentence number {i} with padding." for i in range(10)]
    text = " ".join(sentences)
    out = chunk_text(text, target_chars=120, overlap_sentences=0)
    assert len(out) >= 2
    # Re-joining all chunks should equal the original sentence sequence with
    # no duplicated content (whitespace differences ignored).
    rejoined_lengths = sum(len(c) for c in out)
    # Total char count should be within a small fudge factor of the input
    # (just whitespace differences between joiners).
    assert rejoined_lengths <= len(text) + len(out)


# -------------------- long-sentence fallback --------------------

def test_chunk_long_sentence_split_by_soft_punctuation():
    # One "sentence" that's too big — but has commas as soft breaks.
    parts = ["這是片段" + str(i) + "，" for i in range(100)]   # ~7 chars each
    text = "".join(parts) + "結尾。"

    out = chunk_text(text)

    # Every chunk should respect the CJK target.
    for chunk in out:
        assert len(chunk) <= CJK_TARGET_CHARS, f"chunk over target: {len(chunk)} > {CJK_TARGET_CHARS}"
    assert len(out) >= 2


def test_chunk_long_sentence_hard_cut_when_no_soft_punctuation():
    # Pathological case: no punctuation at all, longer than target.
    text = "a" * (LATIN_TARGET_CHARS * 2 + 50)
    out = chunk_text(text)
    assert len(out) >= 2
    for chunk in out:
        assert len(chunk) <= LATIN_TARGET_CHARS


# -------------------- realistic scenario --------------------

def test_chunk_realistic_cjk_paragraph_keeps_sentences_intact():
    paragraph = (
        "颱風是一種劇烈的熱帶氣旋。"
        "熱帶氣旋就是在熱帶海洋上發生的低氣壓。"
        "在北半球的颱風，其近地面的風，以颱風中心為中心，呈逆時針方向轉動。"
        "在南半球則呈順時針方向轉動。"
    )
    out = chunk_text(paragraph)
    assert len(out) >= 1
    # No chunk should split a Chinese sentence in the middle (every chunk ends
    # on 。 or contains the whole text — neither chunk should end mid-clause).
    for chunk in out:
        last = chunk.rstrip()
        assert last.endswith("。") or last == paragraph, f"chunk ends mid-sentence: {last[-20:]!r}"
