"""raw chunk 切分（定版方案 §3.3）。

按 embedding tokenizer 切分（不按字符数）；默认块长与重叠来自版本化配置；
chunk 必须落在单一章节内；chunk_id 不依赖 ordinal。
"""

from __future__ import annotations

from dataclasses import dataclass

from novelcanon.config.hash import stable_config_hash
from novelcanon.ingestion.normalize import sha256
from novelcanon.retrieval.tokenizer import Tokenizer
from novelcanon.schemas.ids import raw_chunk_id


@dataclass(frozen=True)
class ChunkConfig:
    target_tokens: int = 300
    overlap_ratio: float = 0.15
    min_tokens: int = 50


@dataclass(frozen=True)
class ChunkDraft:
    raw_chunk_id: str
    source_chapter_id: str
    chunking_version: str
    token_start: int
    token_end: int
    char_start: int
    char_end: int
    token_count: int
    content: str
    content_hash: str
    observed_ordinal: int


def chunking_version_for(tokenizer: Tokenizer, config: ChunkConfig) -> str:
    """chunking 版本：tokenizer + 切分配置 + schema 的稳定 hash。

    tokenizer 或配置变化 → 新版本 → 全量重建 + 原子切换（§3.3）。
    """
    return stable_config_hash(
        {
            "tokenizer": tokenizer.tokenizer_id,
            "target_tokens": config.target_tokens,
            "overlap_ratio": config.overlap_ratio,
            "min_tokens": config.min_tokens,
            "schema": "v1",
        }
    )


def chunk_text(
    text: str,
    *,
    source_chapter_id: str,
    observed_ordinal: int,
    tokenizer: Tokenizer,
    chunking_version: str,
    config: ChunkConfig | None = None,
) -> list[ChunkDraft]:
    """把单章文本切为 chunk（token 区间 → char 区间）。

    覆盖保证：chunk 区间拼接覆盖整章文本，无非预期空洞（§14.1）。
    """
    if config is None:
        config = ChunkConfig()
    offsets = tokenizer.token_char_offsets(text)
    total_tokens = len(offsets) - 1
    if total_tokens <= 0:
        return []

    step = max(config.target_tokens, 1)
    overlap = max(int(step * config.overlap_ratio), 1) if step > config.min_tokens else 0

    chunks: list[ChunkDraft] = []
    pos = 0
    while pos < total_tokens:
        end = min(pos + step, total_tokens)
        char_start = offsets[pos]
        char_end = offsets[end]
        content = text[char_start:char_end]
        token_count = end - pos
        # 覆盖保证：末段无论多短都保留（§14.1 无空洞）
        if token_count >= config.min_tokens or end == total_tokens:
            anchor = f"{source_chapter_id}:{pos}"
            chunks.append(
                ChunkDraft(
                    raw_chunk_id=raw_chunk_id(source_chapter_id, chunking_version, anchor),
                    source_chapter_id=source_chapter_id,
                    chunking_version=chunking_version,
                    token_start=pos,
                    token_end=end,
                    char_start=char_start,
                    char_end=char_end,
                    token_count=token_count,
                    content=content,
                    content_hash=sha256(content),
                    observed_ordinal=observed_ordinal,
                )
            )
        if end >= total_tokens:
            break
        pos = max(end - overlap, pos + 1)
    return chunks
