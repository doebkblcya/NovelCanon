"""阶段 03：chunk 切分黄金测试（§3.3 验证项）。"""

from novelcanon.retrieval.chunker import (
    ChunkConfig,
    chunk_text,
    chunking_version_for,
)
from novelcanon.retrieval.tokenizer import FakeTokenizer

TOKENIZER = FakeTokenizer()
CV = chunking_version_for(TOKENIZER, ChunkConfig())


def _chapter_text() -> str:
    return (
        "第一章 雨夜惊变\n"
        "林风在雨夜中踏入青云宗的山门，看见一块巨大的石碑。"
        "守门的弟子打量了他一眼，冷声道：青云宗不收来历不明之人。\n"
        "夜色渐深，山门前的灯火次第亮起。"
    )


def test_chunk_coverage_no_holes() -> None:
    """chunk 覆盖整章（允许重叠，但无空洞；§14.1）。"""
    text = _chapter_text()
    chunks = chunk_text(
        text,
        source_chapter_id="ch_1",
        observed_ordinal=1,
        tokenizer=TOKENIZER,
        chunking_version=CV,
        config=ChunkConfig(target_tokens=40, overlap_ratio=0.15, min_tokens=10),
    )
    assert chunks
    # 覆盖验证：每个 char 至少属于一个 chunk（无空洞）
    covered = [False] * len(text)
    for c in chunks:
        assert 0 <= c.char_start < c.char_end <= len(text)
        for i in range(c.char_start, c.char_end):
            covered[i] = True
    assert all(covered), "chunk 覆盖出现空洞"
    assert chunks[0].raw_chunk_id.startswith("chunk_")


def test_chunk_never_crosses_chapter() -> None:
    """chunk 必须落在单一章节内（§3.3）。"""
    text = _chapter_text()
    chunks = chunk_text(
        text,
        source_chapter_id="ch_2",
        observed_ordinal=2,
        tokenizer=TOKENIZER,
        chunking_version=CV,
    )
    assert all(c.source_chapter_id == "ch_2" for c in chunks)


def test_chunk_id_stable_and_anchor_sensitive() -> None:
    text = _chapter_text()
    a = chunk_text(
        text,
        source_chapter_id="ch_1",
        observed_ordinal=1,
        tokenizer=TOKENIZER,
        chunking_version=CV,
    )
    b = chunk_text(
        text,
        source_chapter_id="ch_1",
        observed_ordinal=1,
        tokenizer=TOKENIZER,
        chunking_version=CV,
    )
    assert [c.raw_chunk_id for c in a] == [c.raw_chunk_id for c in b]
    # tokenizer 变化 → 新 chunking 版本
    assert chunking_version_for(TOKENIZER, ChunkConfig(target_tokens=200)) != CV


def test_fake_tokenizer_offsets() -> None:
    text = "甲乙丙丁"
    offsets = TOKENIZER.token_char_offsets(text)
    assert offsets == [0, 1, 2, 3, 4]  # 每 code point 1 token
    assert TOKENIZER.count(text) == 4
