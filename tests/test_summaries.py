"""阶段 10 分层摘要测试（docs/implementation/10 §7）。

覆盖验证项：
- 章节结构化记忆 → 卷摘要 → 全书摘要（依赖图）；
- 每个摘要保存输入 claim 版本集合/generation/profile/prompt 版本/
  content hash/max observed ordinal/依赖下级摘要版本；
- 输入事实变化 → 依赖摘要按图标记失效并重建；
- max observed ordinal 正确；
- 卷和全书摘要可重建且不泄露 cutoff 后内容（cutoff 过滤记忆）。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, text

from novelcanon.summaries import (
    DeterministicSummarizer,
    HierarchicalReducer,
)
from tests.helpers import seed_active_book


def _reduce(migrated_db: Engine, data: dict) -> object:
    reducer = HierarchicalReducer(
        migrated_db, data["book_id"], summarizer=DeterministicSummarizer()
    )
    return reducer.reduce()


def test_reduce_hierarchy_and_max_ordinal(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """章节 → 卷 → 全书：输入集合/max ordinal/依赖图记录正确。"""
    data = seed_active_book(migrated_db, tmp_path)
    result = _reduce(migrated_db, data)
    assert result.chapter_memories == 3
    assert len(result.volume_summaries) == 1  # 3 章 → 1 卷（<50）
    assert result.book_summary is not None
    vol = result.volume_summaries[0]
    assert vol["max_observed_ordinal"] == 2  # 3 章 → ordinal 0..2
    assert vol["level"] == "volume"
    assert vol["prompt_version"] == "deterministic-v2"
    assert vol["content_hash"]
    versions = json.loads(vol["input_claim_versions"])
    assert versions and all(v.startswith("ver_") for v in versions)
    book = result.book_summary
    assert book["level"] == "book"
    assert book["max_observed_ordinal"] == 2
    deps = json.loads(book["depends_on_summaries"])
    assert deps == [vol["summary_id"]]


def test_reduce_idempotent_reuse(tmp_path: Path, migrated_db: Engine) -> None:
    """输入不变 → 复用（不重建、不重复写入）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _reduce(migrated_db, data)
    r2 = _reduce(migrated_db, data)
    assert r2.reused == 2  # 卷 + 全书复用
    assert r2.rebuilt == 0
    with migrated_db.connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM summary_artifacts"
                " WHERE book_id = :b AND status = 'valid'"
            ),
            {"b": data["book_id"]},
        ).scalar()
    assert n == 2  # 卷 + 全书，无重复


def test_reduce_rebuilds_on_new_input(tmp_path: Path, migrated_db: Engine) -> None:
    """输入 claim 变化 → 卷摘要新版本 + 旧标 stale → 全书按依赖图重建。"""
    data = seed_active_book(migrated_db, tmp_path)
    r1 = _reduce(migrated_db, data)
    old_book_id = r1.book_summary["summary_id"]
    old_vol_id = r1.volume_summaries[0]["summary_id"]

    # 新增一条 claim（第二章 new fact）→ 输入变化
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.payloads import RelationPayload
    from novelcanon.schemas.types import ClaimStatus, Operation
    from novelcanon.storage.repository import Repository

    repo = Repository(migrated_db)
    repo.write_claim(
        ClaimEnvelope(
            fact_id=relation_fact_id("ent_xiaoyan", "师徒", "ent_yaolao"),
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][1],
            observed_ordinal=1,
            world_valid_kind="chapter_proxy",
            world_valid_from=1,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_xiaoyan",
            to_entity_id="ent_yaolao",
            relation_type="师徒",
            relation_raw="萧炎拜药老为师",
        ),
    )
    r2 = _reduce(migrated_db, data)
    assert r2.rebuilt >= 1
    assert r2.volume_summaries[0]["summary_id"] != old_vol_id
    assert r2.book_summary["summary_id"] != old_book_id
    # 旧版本标 stale
    with migrated_db.connect() as conn:
        for sid in (old_vol_id, old_book_id):
            status = conn.execute(
                text("SELECT status FROM summary_artifacts WHERE summary_id = :s"),
                {"s": sid},
            ).scalar()
        assert status == "stale"


def test_reduce_respects_cutoff(tmp_path: Path, migrated_db: Engine) -> None:
    """cutoff 过滤输入：摘要不泄露 cutoff 后内容，max ordinal <= cutoff。"""
    data = seed_active_book(migrated_db, tmp_path)
    reducer = HierarchicalReducer(
        migrated_db, data["book_id"], summarizer=DeterministicSummarizer()
    )
    result = reducer.reduce(cutoff=1)
    # cutoff=1 → 只含 ch1/ch2 的记忆（ordinal 0..1），max ordinal = 1
    assert result.chapter_memories == 2
    assert result.volume_summaries[0]["max_observed_ordinal"] == 1
    assert "异火" not in result.volume_summaries[0]["content"]  # ch3 内容不泄露


def test_reduce_deterministic_content_stable(tmp_path: Path, migrated_db: Engine) -> None:
    """确定性摘要：同输入同内容（content hash 稳定，可重建）。"""
    data = seed_active_book(migrated_db, tmp_path)
    r1 = _reduce(migrated_db, data)
    # 清空后重建（可重建性）：同样输入 → 同 hash
    with migrated_db.begin() as conn:
        conn.execute(
            text("DELETE FROM summary_artifacts WHERE book_id = :b"),
            {"b": data["book_id"]},
        )
    r2 = _reduce(migrated_db, data)
    assert (
        r1.volume_summaries[0]["content_hash"]
        == r2.volume_summaries[0]["content_hash"]
    )
    assert (
        r1.book_summary["content_hash"] == r2.book_summary["content_hash"]
    )


def test_book_summary_consumes_volume_summaries(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """P0-1：全书摘要的生成输入是卷摘要（分层 Reduce），不再展开章节记忆。

    LLM prompt 断言：book 级调用只含卷摘要内容，不含章节记忆 JSON。
    """
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.generation.client import FakeGenerationClient
    from novelcanon.pipeline.ledger import Usage
    from novelcanon.summaries import LLMSummarizer

    fake = FakeGenerationClient(
        {
            "章节记忆": '{"summary":"卷摘要文本","key_events":[],"key_entities":[]}',
            "卷摘要": '{"summary":"全书摘要文本","key_events":[],"key_entities":[]}',
        },
        usage=Usage(input_tokens=80, output_tokens=30, provider="fake", model="m"),
    )
    summarizer = LLMSummarizer(fake, profile_id="p1")
    reducer = HierarchicalReducer(
        migrated_db, data["book_id"], summarizer=summarizer
    )
    result = reducer.reduce()
    assert result.book_summary is not None
    # 两次调用：卷摘要（章节记忆）→ 全书摘要（卷摘要）
    assert len(fake.calls) == 2
    volume_prompt, book_prompt = fake.calls
    assert "章节记忆" in volume_prompt, "卷摘要输入是章节记忆"
    assert "章节记忆" not in book_prompt, "全书摘要不得再次展开章节记忆"
    assert "卷摘要" in book_prompt, "全书摘要输入是卷摘要（分层 Reduce）"
    assert "全书摘要文本" in result.book_summary["content"]


def test_llm_reduce_usage_aggregated(tmp_path: Path, migrated_db: Engine) -> None:
    """P1-5b：LLM Reduce 的 usage 汇总到 SummaryResult.tokens。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.generation.client import FakeGenerationClient
    from novelcanon.pipeline.ledger import Usage
    from novelcanon.summaries import LLMSummarizer

    fake = FakeGenerationClient(
        {
            "章节记忆": '{"summary":"卷摘要","key_events":[],"key_entities":[]}',
            "卷摘要": '{"summary":"全书摘要","key_events":[],"key_entities":[]}',
        },
        usage=Usage(input_tokens=100, output_tokens=50, provider="fake", model="m"),
    )
    reducer = HierarchicalReducer(
        migrated_db,
        data["book_id"],
        summarizer=LLMSummarizer(fake, profile_id="p1"),
    )
    result = reducer.reduce()
    # 卷 + 全书两次调用
    assert result.tokens.input_tokens == 200
    assert result.tokens.output_tokens == 100


def test_reducer_reuse_compares_profile_and_schema(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """P1（复审）：幂等复用判定比较 generation profile 与 schema 版本。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.generation.client import FakeGenerationClient
    from novelcanon.pipeline.ledger import Usage
    from novelcanon.summaries import LLMSummarizer

    fake = FakeGenerationClient(
        {
            "章节记忆": '{"summary":"卷A","key_events":[],"key_entities":[]}',
            "卷摘要": '{"summary":"书B","key_events":[],"key_entities":[]}',
        },
        usage=Usage(input_tokens=10, output_tokens=5, provider="fake", model="m"),
    )
    r1 = HierarchicalReducer(
        migrated_db,
        data["book_id"],
        summarizer=LLMSummarizer(fake, profile_id="p-v1", prompt_version="pv1"),
    ).reduce()
    assert r1.rebuilt >= 2
    # 同 profile/schema → 复用
    r2 = HierarchicalReducer(
        migrated_db,
        data["book_id"],
        summarizer=LLMSummarizer(fake, profile_id="p-v1", prompt_version="pv1"),
    ).reduce()
    assert r2.reused >= 2, "同 profile/schema 应复用"
    # profile/prompt 变化：内容相同 → 同 summary_id → 恢复分支更新配置
    # （不误复用旧配置；p-v2 配置必须写入 valid 行）
    HierarchicalReducer(
        migrated_db,
        data["book_id"],
        summarizer=LLMSummarizer(fake, profile_id="p-v2", prompt_version="pv2"),
    ).reduce()
    with migrated_db.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text(
                "SELECT generation_profile_id, prompt_version FROM summary_artifacts"
                " WHERE book_id = :b AND status = 'valid'"
            ),
            {"b": data["book_id"]},
        ).fetchall()
    assert ("p-v2", "pv2") in row, (
        f"profile/prompt 变化后不得保留旧配置：{row}"
    )


def test_reducer_restore_updates_metadata(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """P1（复审）：恢复内容相同的历史摘要时补全 max ordinal/profile/schema。"""
    data = seed_active_book(migrated_db, tmp_path)
    reducer = HierarchicalReducer(
        migrated_db, data["book_id"], summarizer=DeterministicSummarizer()
    )
    r1 = reducer.reduce()
    vol_id = r1.volume_summaries[0]["summary_id"]
    # 篡改元数据后强制重建 → 恢复分支应补全
    from sqlalchemy import text

    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE summary_artifacts SET max_observed_ordinal = 999,"
                " generation_profile_id = 'stale-prof', schema_version = 'old'"
                " WHERE summary_id = :id"
            ),
            {"id": vol_id},
        )
    r2 = reducer.reduce()
    assert r2.reused >= 1, "输入未变应复用（不重跑）"
    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT max_observed_ordinal, generation_profile_id, schema_version"
                " FROM summary_artifacts WHERE summary_id = :id"
            ),
            {"id": vol_id},
        ).fetchone()
    assert row[0] == 2, f"max ordinal 应恢复为真实值：{row}"
    assert row[1] is None, "确定性模式 profile 应为 NULL"
    assert row[2] == "reducer-v1"
