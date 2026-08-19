"""阶段 02 初始 schema：核心事实契约表。

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-19

覆盖定版方案 §8.1 中与事实契约相关的核心表（books / volumes / chapters /
extraction_runs / entities / claims 族 / evidence / event_links /
catalog）。raw_chunks / embedding_records / run_checkpoints /
compression_segments / summary_artifacts 分别在阶段 03/04/10 的 migration 中加入。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from novelcanon.schemas.catalog import EVENT_ONTOLOGY_V1, RELATION_ONTOLOGY_V1, STATE_CATALOG_V1
from novelcanon.schemas.types import ClaimType, EventLinkType

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_CLAIM_TYPES = [t.value for t in ClaimType]
_EVENT_LINK_TYPES = [t.value for t in EventLinkType]


def _check_claim_type() -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "claim_type IN ('relation','event','state','org',"
        " 'foreshadowing','event_link','term_definition')",
        name="ck_claims_claim_type",
    )


def upgrade() -> None:
    # ── 书籍与章节 ───────────────────────────────────────────────
    op.create_table(
        "books",
        sa.Column("book_id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("source_format", sa.Text, nullable=True),
        sa.Column("source_path", sa.Text, nullable=True),
        sa.Column("raw_content_hash", sa.Text, nullable=True),
        sa.Column("normalized_content_hash", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    op.create_table(
        "volumes",
        sa.Column("volume_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("start_chapter_id", sa.Text, nullable=True),
        sa.Column("end_chapter_id", sa.Text, nullable=True),
        sa.Column("grouping_version", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    op.create_table(
        "chapters",
        sa.Column("chapter_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("char_start", sa.Integer, nullable=True),
        sa.Column("char_end", sa.Integer, nullable=True),
        sa.Column("content_hash", sa.Text, nullable=True),
        sa.Column("volume_id", sa.Text, sa.ForeignKey("volumes.volume_id"), nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("book_id", "ordinal", name="uq_chapters_book_ordinal"),
    )
    op.create_index("ix_chapters_book_id", "chapters", ["book_id"])

    op.create_table(
        "extraction_runs",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("book_id", sa.Text, sa.ForeignKey("books.book_id"), nullable=False),
        sa.Column(
            "status",
            sa.Text,
            sa.CheckConstraint(
                "status IN ('running','failed','active','superseded')",
                name="ck_runs_status",
            ),
            nullable=False,
            server_default="running",
        ),
        sa.Column("input_hash", sa.Text, nullable=True),
        sa.Column("pipeline_version", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("schema_version", sa.Text, nullable=True),
        sa.Column("compression_version", sa.Text, nullable=True),
        sa.Column("generation_profile_id", sa.Text, nullable=True),
        sa.Column("embedding_profile_id", sa.Text, nullable=True),
        sa.Column("config_hash", sa.Text, nullable=True),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_runs_book_status", "extraction_runs", ["book_id", "status"])

    # ── 实体 ────────────────────────────────────────────────────
    op.create_table(
        "entities",
        sa.Column("canonical_id", sa.Text, primary_key=True),
        sa.Column("canonical_name", sa.Text, nullable=False),
        sa.Column(
            "tier",
            sa.Text,
            sa.CheckConstraint(
                "tier IN ('core','major','minor','one_off')", name="ck_entities_tier"
            ),
            nullable=False,
            server_default="minor",
        ),
        sa.Column(
            "importance_score",
            sa.REAL,
            sa.CheckConstraint("importance_score >= 0 AND importance_score <= 100"),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_by_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    # ── claims 公共 envelope（append-only）─────────────────────
    op.create_table(
        "claims",
        sa.Column("fact_id", sa.Text, nullable=False),
        sa.Column("claim_version_id", sa.Text, primary_key=True),
        sa.Column("claim_type", sa.Text, _check_claim_type(), nullable=False),
        sa.Column(
            "operation",
            sa.Text,
            sa.CheckConstraint(
                "operation IN ('assert','update','retract')", name="ck_claims_operation"
            ),
            nullable=False,
        ),
        sa.Column(
            "supersedes_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id"),
            nullable=True,
        ),
        sa.Column(
            "confidence",
            sa.REAL,
            sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "claim_status",
            sa.Text,
            sa.CheckConstraint(
                "claim_status IN ('unverified','supported','contested','rejected')",
                name="ck_claims_status",
            ),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column(
            "observed_chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=True,
        ),
        sa.Column("observed_ordinal", sa.Integer, nullable=True),
        sa.Column(
            "world_valid_kind",
            sa.Text,
            sa.CheckConstraint(
                "world_valid_kind IN ('story_time','chapter_proxy','unknown')",
                name="ck_claims_world_kind",
            ),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("world_valid_from", sa.Integer, nullable=True),
        sa.Column("world_valid_to", sa.Integer, nullable=True),
        sa.Column(
            "world_valid_confidence",
            sa.REAL,
            sa.CheckConstraint("world_valid_confidence >= 0 AND world_valid_confidence <= 1"),
            nullable=True,
        ),
        sa.Column(
            "created_by_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("prompt_version", sa.Text, nullable=False, server_default=""),
        sa.Column("pipeline_version", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("primary_evidence_id", sa.Text, nullable=True),
    )
    op.create_index("ix_claims_observed_ordinal", "claims", ["observed_ordinal"])
    op.create_index("ix_claims_supersedes", "claims", ["supersedes_version_id"])
    op.create_index("ix_claims_fact_id", "claims", ["fact_id"])
    op.create_index("ix_claims_run", "claims", ["created_by_run_id"])

    op.create_table(
        "claim_observations",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "extraction_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "claim_version_id", "extraction_run_id", name="pk_claim_observations"
        ),
    )

    # ── 类型专属一对一子表（§4.2：不塞 JSON 大字段）────────────
    op.create_table(
        "relation_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "from_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column("to_entity_id", sa.Text, sa.ForeignKey("entities.canonical_id"), nullable=False),
        sa.Column("relation_type", sa.Text, nullable=False),
        sa.Column("relation_raw", sa.Text, nullable=False, server_default=""),
        sa.Column("direction", sa.Text, nullable=False, server_default="undirected"),
    )
    op.create_index("ix_relation_from_type", "relation_claims", ["from_entity_id", "relation_type"])
    op.create_index("ix_relation_to", "relation_claims", ["to_entity_id"])

    op.create_table(
        "event_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "location_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=True,
        ),
        sa.Column("sequence_in_chapter", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "narrative_weight",
            sa.REAL,
            sa.CheckConstraint("narrative_weight >= 0 AND narrative_weight <= 1"),
            nullable=False,
            server_default="0.5",
        ),
    )
    op.create_index("ix_event_type", "event_claims", ["event_type"])

    op.create_table(
        "event_participants",
        sa.Column(
            "event_claim_version_id",
            sa.Text,
            sa.ForeignKey("event_claims.claim_version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Text, sa.ForeignKey("entities.canonical_id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default=""),
        sa.UniqueConstraint(
            "event_claim_version_id", "entity_id", "role", name="uq_event_participants"
        ),
    )
    op.create_index(
        "ix_event_participants_entity",
        "event_participants",
        ["entity_id", "event_claim_version_id"],
    )

    op.create_table(
        "state_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("raw_value", sa.Text, nullable=True),
        sa.Column(
            "target_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_state_field", "state_claims", ["field", "value"])

    op.create_table(
        "org_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "org_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column(
            "member_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "action",
            sa.Text,
            sa.CheckConstraint(
                "action IN ('join','leave','promote','demote','found','dissolve')",
                name="ck_org_action",
            ),
            nullable=False,
            server_default="join",
        ),
    )
    op.create_index("ix_org_member", "org_claims", ["member_entity_id", "org_entity_id"])

    op.create_table(
        "foreshadow_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("clue_anchor", sa.Text, nullable=False),
        sa.Column("related_entity_ids", sa.Text, nullable=False, server_default="[]"),
    )

    op.create_table(
        "terms",
        sa.Column("term_id", sa.Text, primary_key=True),
        sa.Column("canonical_name", sa.Text, nullable=False),
        sa.Column("first_observed_ordinal", sa.Integer, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    op.create_table(
        "term_definition_claims",
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("term_id", sa.Text, sa.ForeignKey("terms.term_id"), nullable=False),
        sa.Column("definition", sa.Text, nullable=False),
        sa.Column("first_observed_ordinal", sa.Integer, nullable=True),
    )

    # ── 别名 / 提及 / 合并审计 ─────────────────────────────────
    op.create_table(
        "entity_alias_claims",
        sa.Column("claim_version_id", sa.Text, primary_key=True),
        sa.Column("alias_fact_id", sa.Text, nullable=False),
        sa.Column(
            "canonical_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column("surface_name", sa.Text, nullable=False),
        sa.Column(
            "operation",
            sa.Text,
            sa.CheckConstraint("operation IN ('assert','retract')", name="ck_alias_op"),
            nullable=False,
            server_default="assert",
        ),
        sa.Column(
            "supersedes_version_id",
            sa.Text,
            sa.ForeignKey("entity_alias_claims.claim_version_id"),
            nullable=True,
        ),
        sa.Column("observed_ordinal", sa.Integer, nullable=True),
        sa.Column(
            "observed_chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=True,
        ),
        sa.Column(
            "created_by_run_id",
            sa.Text,
            sa.ForeignKey("extraction_runs.run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_alias_surface", "entity_alias_claims", ["surface_name"])
    op.create_index(
        "ix_alias_canonical", "entity_alias_claims", ["canonical_id", "observed_ordinal"]
    )

    op.create_table(
        "entity_mentions",
        sa.Column("mention_id", sa.Text, primary_key=True),
        sa.Column("chapter_id", sa.Text, sa.ForeignKey("chapters.chapter_id"), nullable=False),
        sa.Column("surface_name", sa.Text, nullable=False),
        sa.Column("char_start", sa.Integer, nullable=True),
        sa.Column("char_end", sa.Integer, nullable=True),
        sa.Column(
            "canonical_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=True,
        ),
        sa.Column("run_id", sa.Text, sa.ForeignKey("extraction_runs.run_id"), nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_mentions_surface", "entity_mentions", ["surface_name"])

    op.create_table(
        "entity_merge_audit",
        sa.Column("audit_id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column(
            "action",
            sa.Text,
            sa.CheckConstraint("action IN ('merge','split')", name="ck_merge_action"),
            nullable=False,
        ),
        sa.Column(
            "from_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column(
            "to_entity_id",
            sa.Text,
            sa.ForeignKey("entities.canonical_id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("extraction_runs.run_id"), nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    # ── 证据（source span 唯一持有者）─────────────────────────
    op.create_table(
        "claim_evidence",
        sa.Column("evidence_id", sa.Text, primary_key=True),
        sa.Column(
            "claim_version_id",
            sa.Text,
            sa.ForeignKey("claims.claim_version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_stance",
            sa.Text,
            sa.CheckConstraint(
                "evidence_stance IN ('supports','refutes','unclear')",
                name="ck_evidence_stance",
            ),
            nullable=False,
        ),
        sa.Column(
            "evidence_type",
            sa.Text,
            sa.CheckConstraint(
                "evidence_type IN ('direct','contextual','inferred')",
                name="ck_evidence_type",
            ),
            nullable=False,
            server_default="direct",
        ),
        sa.Column("chapter_id", sa.Text, sa.ForeignKey("chapters.chapter_id"), nullable=False),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("span_hash", sa.Text, nullable=False),
        sa.Column(
            "literal_match_rate",
            sa.REAL,
            sa.CheckConstraint("literal_match_rate >= 0 AND literal_match_rate <= 1"),
            nullable=False,
            server_default="0",
        ),
        sa.Column("verification_method", sa.Text, nullable=False, server_default=""),
        sa.Column("verification_run_id", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "claim_version_id",
            "chapter_id",
            "char_start",
            "char_end",
            "span_hash",
            name="uq_evidence_span",
        ),
    )
    op.create_index("ix_evidence_chapter", "claim_evidence", ["chapter_id"])

    # ── 事件链接（一等事实表，§5.3）───────────────────────────
    op.create_table(
        "event_links",
        sa.Column("claim_version_id", sa.Text, primary_key=True),
        sa.Column("fact_id", sa.Text, nullable=False),
        sa.Column(
            "source_event_id",
            sa.Text,
            sa.ForeignKey("event_claims.claim_version_id"),
            nullable=False,
        ),
        sa.Column(
            "target_event_id",
            sa.Text,
            sa.ForeignKey("event_claims.claim_version_id"),
            nullable=False,
        ),
        sa.Column(
            "relation_type",
            sa.Text,
            sa.CheckConstraint(
                "relation_type IN ('causes','enables','prevents')",
                name="ck_event_link_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.REAL,
            sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "claim_status",
            sa.Text,
            sa.CheckConstraint(
                "claim_status IN ('unverified','supported','contested','rejected')",
                name="ck_event_link_status",
            ),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column(
            "observed_chapter_id",
            sa.Text,
            sa.ForeignKey("chapters.chapter_id"),
            nullable=True,
        ),
        sa.Column("observed_ordinal", sa.Integer, nullable=True),
        sa.Column(
            "supersedes_version_id",
            sa.Text,
            sa.ForeignKey("event_links.claim_version_id"),
            nullable=True,
        ),
        sa.Column("primary_evidence_id", sa.Text, nullable=True),
    )
    op.create_index("ix_event_links_source", "event_links", ["source_event_id"])
    op.create_index("ix_event_links_target", "event_links", ["target_event_id"])
    op.create_index("ix_event_links_ordinal", "event_links", ["observed_ordinal"])

    # ── ontology / state catalog ───────────────────────────────
    op.create_table(
        "state_catalog",
        sa.Column("field", sa.Text, primary_key=True),
        sa.Column("value_type", sa.Text, nullable=False),
        sa.Column("multivalued", sa.Integer, nullable=False, server_default="0"),
        sa.Column("target_required", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enum_values", sa.Text, nullable=False, server_default="[]"),
        sa.Column("unit", sa.Text, nullable=True),
        sa.Column("sort_values", sa.Text, nullable=False, server_default="[]"),
        sa.Column("applicable_entity_types", sa.Text, nullable=False, server_default="[]"),
        sa.Column("catalog_version", sa.Text, nullable=False),
    )

    op.create_table(
        "ontology_versions",
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("claim_type", sa.Text, _check_claim_type(), nullable=False),
        sa.Column("allowed_values", sa.Text, nullable=False, server_default="[]"),
        sa.Column("schema_version", sa.Text, nullable=False, server_default="v1"),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("version", "claim_type", name="pk_ontology_versions"),
    )

    _seed_catalog()

    # ── active 视图（阶段 02：默认查询只读激活 run 的数据）────────
    # _rowid 暴露底层写入序，供「当前版本 = 每 fact 最新版本」推导。
    op.execute(
        "CREATE VIEW IF NOT EXISTS v_active_claims AS "
        "SELECT c.rowid AS _rowid, c.* FROM claims c "
        "JOIN extraction_runs r ON c.created_by_run_id = r.run_id "
        "WHERE r.status = 'active'"
    )


def _seed_catalog() -> None:
    """初始种子：ontology 与 state catalog 初始版本（阶段 02）。"""
    import json

    conn = op.get_bind()

    for entry in STATE_CATALOG_V1:
        conn.execute(
            sa.text(
                "INSERT INTO state_catalog (field, value_type, multivalued, target_required,"
                " enum_values, unit, sort_values, applicable_entity_types, catalog_version)"
                " VALUES (:field, :value_type, :multivalued, :target_required,"
                " :enum_values, :unit, :sort_values, :applicable_entity_types, :catalog_version)"
            ),
            {
                "field": entry.field,
                "value_type": entry.value_type,
                "multivalued": int(entry.multivalued),
                "target_required": int(entry.target_required),
                "enum_values": json.dumps(entry.enum_values, ensure_ascii=False),
                "unit": entry.unit,
                "sort_values": json.dumps(entry.sort_values, ensure_ascii=False),
                "applicable_entity_types": json.dumps(
                    entry.applicable_entity_types, ensure_ascii=False
                ),
                "catalog_version": entry.catalog_version,
            },
        )

    conn.execute(
        sa.text(
            "INSERT INTO ontology_versions (version, claim_type, allowed_values,"
            " schema_version, updated_at)"
            " VALUES (:v, :t, :a, :s, :ts)"
        ),
        {
            "v": "v1",
            "t": "relation",
            "a": json.dumps(RELATION_ONTOLOGY_V1, ensure_ascii=False),
            "s": "v1",
            "ts": "2026-08-19T00:00:00Z",
        },
    )
    conn.execute(
        sa.text(
            "INSERT INTO ontology_versions (version, claim_type, allowed_values,"
            " schema_version, updated_at)"
            " VALUES (:v, :t, :a, :s, :ts)"
        ),
        {
            "v": "v1",
            "t": "event",
            "a": json.dumps(EVENT_ONTOLOGY_V1, ensure_ascii=False),
            "s": "v1",
            "ts": "2026-08-19T00:00:00Z",
        },
    )


def downgrade() -> None:
    """按依赖逆序删除全部表（阶段 02 初始版本无数据保留要求）。"""
    op.execute("DROP VIEW IF EXISTS v_active_claims")
    op.drop_table("claim_observations")
    op.drop_table("ontology_versions")
    op.drop_table("state_catalog")
    op.drop_table("event_links")
    op.drop_table("claim_evidence")
    op.drop_table("entity_merge_audit")
    op.drop_table("entity_mentions")
    op.drop_table("entity_alias_claims")
    op.drop_table("term_definition_claims")
    op.drop_table("terms")
    op.drop_table("foreshadow_claims")
    op.drop_table("org_claims")
    op.drop_table("state_claims")
    op.drop_table("event_participants")
    op.drop_table("event_claims")
    op.drop_table("relation_claims")
    op.drop_table("claims")
    op.drop_table("entities")
    op.drop_table("extraction_runs")
    op.drop_table("chapters")
    op.drop_table("volumes")
    op.drop_table("books")
