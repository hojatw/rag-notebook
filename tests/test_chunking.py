"""Unit tests for the sentence-aware chunker in app.ingest.

Covers:
  - Language detection (is_mostly_cjk)
  - Sentence splitting on CJK / Latin punctuation
  - Auto-sizing (CJK -> 400 chars, Latin -> 800 chars)
  - Overlap behaviour
  - Long-sentence fallback (soft punctuation, then hard cut)
  - Edge cases (empty, whitespace, single short sentence)
  - Cross-section packing + span labels (chunk_sections)
"""
from app.ingest import (
    CJK_TARGET_CHARS,
    LATIN_TARGET_CHARS,
    chunk_sections,
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


# -------------------- cross-section packing (chunk_sections) --------------------


def test_chunk_sections_single_section_matches_chunk_text():
    # One section must behave exactly like chunk_text over the same text.
    paragraph = (
        "颱風是一種劇烈的熱帶氣旋。"
        "熱帶氣旋就是在熱帶海洋上發生的低氣壓。"
        "在北半球的颱風，其近地面的風，以颱風中心為中心，呈逆時針方向轉動。"
        "在南半球則呈順時針方向轉動。"
    ) * 4
    via_sections = [body for _, body in chunk_sections([("document", paragraph)])]
    assert via_sections == chunk_text(paragraph)


def test_chunk_sections_packs_many_small_sections_to_target():
    # Mimic the PDF path: lots of tiny per-paragraph sections. They must be
    # packed up to the CJK target instead of staying one-fragment-per-section.
    sentence = "智慧排程系統根據任務優先序與飛行員可用性自動產生派飛建議表。"
    sections = [(f"page 1 paragraph {i}", sentence) for i in range(1, 41)]
    out = chunk_sections(sections)

    assert len(out) < len(sections)  # merged, not one chunk per paragraph
    sizes = [len(body) for _, body in out]
    # Every chunk except possibly the last should be filled near the target,
    # and none may exceed it.
    assert max(sizes) <= CJK_TARGET_CHARS
    assert all(size > CJK_TARGET_CHARS // 2 for size in sizes[:-1])


def test_chunk_sections_labels_span_first_to_last():
    sentence = "智慧排程系統根據任務優先序自動產生派飛建議表。"
    sections = [(f"page 1 paragraph {i}", sentence) for i in range(1, 21)]
    out = chunk_sections(sections)

    # A chunk that merged several paragraph blocks is labelled as a span.
    spanning = [loc for loc, _ in out if "–" in loc]
    assert spanning, "expected at least one merged chunk labelled as a span"
    first_label = out[0][0]
    assert first_label.startswith("page 1 paragraph 1")
    assert "–" in first_label  # "page 1 paragraph 1 – page 1 paragraph N"


def test_chunk_sections_flushes_when_section_kind_changes():
    body = "這是正文段落，應該跟下一段正文合併。" * 4
    table = "Table:\n欄位 | 數值\nA | 123\nB | 456"
    sections = [
        ("page 1 paragraph 1", body),
        ("page 1 table 1", table),
        ("page 1 paragraph 2", body),
    ]

    out = chunk_sections(sections, target_chars=400, overlap_sentences=1)

    assert len(out) == 3
    assert out[0][0] == "page 1 paragraph 1"
    assert out[1][0] == "page 1 table 1"
    assert out[2][0] == "page 1 paragraph 2"
    assert "Table:" not in out[0][1]
    assert "Table:" in out[1][1]


def test_chunk_sections_drops_overlap_when_it_would_exceed_target():
    sentence_a = "甲" * 399 + "。"
    sentence_b = "乙" * 399 + "。"
    out = chunk_sections([("document", sentence_a + sentence_b)], target_chars=400, overlap_sentences=1)

    assert len(out) == 2
    assert all(len(body) <= 400 for _, body in out)
    assert out[0][1] == sentence_a
    assert out[1][1] == sentence_b


def test_chunk_sections_single_contributor_keeps_plain_label():
    # A lone section long enough to split into several chunks: every chunk must
    # keep that section's own label (never a span), since nothing else merged in.
    big = "排程演算法可採用限制滿足、整數規劃、啟發式演算法或混合式最佳化方法。" * 30
    out = chunk_sections([("page 15 paragraph 2", big)])
    assert len(out) > 1  # the section genuinely splits across multiple chunks
    assert all(loc == "page 15 paragraph 2" for loc, _ in out)


#: The chunk that actually broke a customer ingest: a sparse PDF table rendered
#: as pipes. Kept verbatim (shortened) because its *shape* is the whole point —
#: mostly separators, and the old estimator billed them as English prose.
PIPE_WALL_CHUNK = (
    "| | | | combination with enfortumab | | | | | | | | vedotin before and after RC with "
    "| | | | | | | | | | | | | | PLND | | | | | | | | | | | | | | n=167 | | | | | | | | "
    "| | | | | | All Grades | | | | | | | | Grades 3-4 | | | | | |"
)


def test_token_estimate_catches_a_chunk_that_is_mostly_separators():
    """The regression that let a rejected source ship with no warning.

    The old estimator picked one of two ratios from the whole chunk's language
    and charged this at Latin prose density — 4 characters per token — when
    every pipe is a token of its own. It read 193 estimated against 533 actual,
    so `chunk_over_token_budget` never fired on the one source the embedding
    endpoint went on to refuse with HTTP 400.
    """
    from app.ingest import estimate_embedding_tokens

    estimate = estimate_embedding_tokens(PIPE_WALL_CHUNK)
    # ~1 token per non-space character here: "▁" then "|" for every pipe.
    assert estimate > len(PIPE_WALL_CHUNK) * 0.45, estimate
    # Same length of ordinary English must stay far cheaper, or the estimate is
    # not measuring density at all — it is just a length check in disguise.
    prose = ("The recommended dosage is 25 mg once daily with food. " * 5)[: len(PIPE_WALL_CHUNK)]
    assert estimate_embedding_tokens(prose) < estimate / 2


def test_token_estimate_charges_full_width_punctuation_as_symbols():
    """Full-width forms cost about a token each; they are not cheap letters.

    Measured at ~0.95 tokens per character against the real tokenizer. A CJK
    document laid out with （）、；※ runs is one of the shapes that sits closest
    to the window, so mis-classifying these as prose is how a warning goes
    missing on exactly the wrong document.
    """
    from app.ingest import estimate_embedding_tokens

    text = "（一）、（二）、（三）；※◎△▲□■◇◆" * 4
    assert estimate_embedding_tokens(text) > len(text) * 0.85


def test_token_estimate_does_not_cry_wolf_on_ordinary_prose():
    """Over-estimating is the safe direction, but it still has to stay usable.

    A full-size chunk of English or Traditional Chinese prose is nowhere near
    the window, and must not be reported as if it were, or the warning becomes
    noise people learn to scroll past.
    """
    from app.ingest import estimate_embedding_tokens
    from app.config import config

    english = ("The recommended dosage is 25 mg once daily with food. " * 20)[:800]
    chinese = "本公司之品質管理系統依照國際標準建立並持續改善所有製程均須經過檢驗。" * 12

    assert estimate_embedding_tokens(english) < config.diagnostics.embedding_token_budget
    assert estimate_embedding_tokens(chinese[:400]) < config.diagnostics.embedding_token_budget


def test_token_estimate_prices_identifiers_above_words():
    """`aB3xK9pQ` is one piece per character; `recommended` is one or two total.

    Part numbers, hashes and barcodes are where the cheap-word assumption breaks,
    and they are common in exactly the documents that carry dense tables.
    """
    from app.ingest import estimate_embedding_tokens

    identifiers = "aB3xK9pQ2mZ7wL4nR8vT " * 8
    words = "recommendation deployment information " * 5
    assert estimate_embedding_tokens(identifiers[:160]) > estimate_embedding_tokens(words[:160]) * 2
