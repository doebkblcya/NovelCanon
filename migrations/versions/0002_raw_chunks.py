"""阶段 03：raw chunks、索引版本、FTS（影子列/trigram）、向量元数据。

Revision ID: 0002_raw_chunks
Revises: 0001_initial

- raw_chunks：按 embedding tokenizer 切分（token 区间 + char 区间 + 原文）；
- index_versions：chunking/向量配置版本，active 唯一（原子切换，§3.3）；
- fts_chunks：预分词影子列（jieba 空格拼接，ADR 决策）；
- fts_chunks_trigram：原文 trigram（二字人名之外的子串召回）；
- embedding_records：向量元数据（向量本体在 vec0 虚表或 BLOB，见 ADR-0006）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_raw_chunks"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 全书规范化文本（SQLite 唯一权威数据源：索引/证据无需重新解析文件）
    op.add_column("books", sa.Column("normalized_text", sa.Text, nullable=True))

    op.create_table(
        "index_versions",
        sa.Column("index_version_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column("chunking_version", sa.Text, nullable=False),
        sa.Column("embedding_profile_id", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint("status IN ('building','active','retired')"),
            nullable=False,
            server_default="building",
        ),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_index_versions_book", "index_versions", ["book_id", "status"])

    op.create_table(
        "raw_chunks",
        sa.Column("raw_chunk_id", sa.Text, primary_key=True),
        sa.Column(
            "source_chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=False,
        ),
        sa.Column("chunking_version", sa.Text, nullable=False),
        sa.Column(
            "index_version_id",
            sa.Text,
            sa.ForeignKey("index_versions.index_version_id"),
            nullable=False,
        ),
        sa.Column("token_start", sa.Integer, nullable=False),
        sa.Column("token_end", sa.Integer, nullable=False),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("embedding_profile_id", sa.Text, nullable=True),
        sa.Column("observed_ordinal", sa.Integer, nullable=False),
    )
    op.create_index("ix_chunks_chapter", "raw_chunks", ["source_chapter_id"])
    op.create_index(
        "ix_chunks_index_version", "raw_chunks", ["index_version_id", "observed_ordinal"]
    )

    # 预分词影子列 FTS（jieba 空格拼接；UNINDEXED 列供过滤）
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
        " raw_chunk_id UNINDEXED, book_id UNINDEXED, observed_ordinal UNINDEXED,"
        " content_ws)"
    )
    # trigram FTS（存原文，tokenize='trigram'）
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks_trigram USING fts5("
        " raw_chunk_id UNINDEXED, book_id UNINDEXED, observed_ordinal UNINDEXED,"
        " content, tokenize='trigram')"
    )

    op.create_table(
        "embedding_records",
        sa.Column("record_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column(
            "raw_chunk_id",
            sa.Text,
            sa.ForeignKey("raw_chunks.raw_chunk_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("book_id", sa.Text, nullable=False),
        sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column(
            "index_version_id",
            sa.Text,
            sa.ForeignKey("index_versions.index_version_id"),
            nullable=False,
        ),
        sa.Column("vector", sa.LargeBinary, nullable=True),  # BruteForce 基线用
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint(
            "raw_chunk_id", "profile_id", "index_version_id", name="uq_embedding_chunk"
        ),
    )
    op.create_index(
        "ix_embedding_index_version", "embedding_records", ["index_version_id", "book_id"]
    )


def downgrade() -> None:
    op.drop_table("embedding_records")
    op.execute("DROP TABLE IF EXISTS fts_chunks_trigram")
    op.execute("DROP TABLE IF EXISTS fts_chunks")
    op.drop_table("raw_chunks")
    op.drop_table("index_versions")
    op.drop_column("books", "normalized_text")
