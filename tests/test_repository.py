"""数据契约黄金测试（docs/implementation/02 第 6 节）。

覆盖：重复观察幂等 / 状态更新版本链 / 撤回 / contested 聚合 /
实体合并审计 / 同名不误合并 / 多书隔离 / failed run 不可见 / 迁移与索引。
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, text

from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import (
    alias_fact_id,
    event_fact_id,
    evidence_id,
    new_uuid_id,
    relation_fact_id,
    state_fact_id,
)
from novelcanon.schemas.memory import AliasClaim, EntityRecord, EvidenceRecord
from novelcanon.schemas.payloads import EventPayload, RelationPayload, StatePayload
from novelcanon.schemas.types import (
    ClaimStatus,
    EvidenceStance,
    Operation,
    RunStatus,
)
from novelcanon.storage.evidence_policy import aggregate_claim_status
from novelcanon.storage.repository import Repository

TS = "2026-08-19T00:00:00Z"


def _seed_book_chapter(
    repo: Repository, book_id: str | None = None, chapter_id: str | None = None
) -> tuple[str, str]:
    book = book_id or new_uuid_id("book")
    ch = chapter_id or new_uuid_id("ch")
    repo.create_book(book, "测试书", source_format="epub")
    repo.create_chapter(ch, book, 1, title="第一章", char_start=0, char_end=100)
    return book, ch


def _envelope(
    run_id: str,
    fact_id: str,
    *,
    op: Operation = Operation.ASSERT,
    chapter_id: str | None = None,
    ordinal: int | None = None,
    claim_type: str = "relation",
) -> ClaimEnvelope:
    return ClaimEnvelope(
        fact_id=fact_id,
        claim_version_id="",  # repo 按幂等键计算
        claim_type=claim_type,
        operation=op,
        observed_chapter_id=chapter_id,
        observed_ordinal=ordinal,
        created_by_run_id=run_id,
        created_at=TS,
    )


def _ensure_entity(repo: Repository, run_id: str, entity_id: str) -> None:
    """relation 子表外键要求实体先存在（契约：canonical 实体先于关系写入）。"""
    repo.upsert_entity(
        EntityRecord(canonical_id=entity_id, canonical_name=entity_id, created_by_run_id=run_id)
    )


def _write_relation(
    repo: Repository,
    run_id: str,
    fact_id: str,
    f: str,
    t: str,
    rtype: str,
    *,
    chapter_id: str | None = None,
    ordinal: int | None = None,
    op: Operation = Operation.ASSERT,
):
    _ensure_entity(repo, run_id, f)
    _ensure_entity(repo, run_id, t)
    env = _envelope(run_id, fact_id, op=op, chapter_id=chapter_id, ordinal=ordinal)
    return repo.write_claim(
        env,
        RelationPayload(from_entity_id=f, to_entity_id=t, relation_type=rtype, relation_raw=rtype),
    )


def _write_state(
    repo: Repository,
    run_id: str,
    fact_id: str,
    field: str,
    value: str,
    *,
    subject_entity_id: str,
    chapter_id: str | None = None,
    ordinal: int | None = None,
    op: Operation = Operation.ASSERT,
):
    # 状态主体必须是已消歧实体（0005 触发器等价 FK 约束）
    _ensure_entity(repo, run_id, subject_entity_id)
    env = _envelope(
        run_id, fact_id, op=op, chapter_id=chapter_id, ordinal=ordinal, claim_type="state"
    )
    return repo.write_claim(
        env,
        StatePayload(
            field=field,
            value=value,
            raw_value=value,
            subject_entity_id=subject_entity_id,
        ),
    )


# ── 黄金场景 1：同一事实重复观察 ────────────────────────────────


def test_repeated_observation_reuses_version(repo: Repository) -> None:
    book, ch = _seed_book_chapter(repo)
    run_a, run_b = new_uuid_id("run"), new_uuid_id("run")
    repo.start_run(run_a, book)
    repo.start_run(run_b, book)

    fact = relation_fact_id("e1", "师徒", "e2")
    r1 = _write_relation(repo, run_a, fact, "e1", "e2", "师徒", chapter_id=ch, ordinal=1)
    r2 = _write_relation(repo, run_b, fact, "e1", "e2", "师徒", chapter_id=ch, ordinal=1)

    assert r1.is_new
    assert not r2.is_new
    assert r1.claim_version_id == r2.claim_version_id
    with repo._engine.connect() as conn:  # noqa: SLF001
        claims = conn.execute(text("SELECT count(*) FROM claims")).scalar()
        obs = conn.execute(text("SELECT count(*) FROM claim_observations")).scalar()
    assert claims == 1
    assert obs == 2


# ── 黄金场景 2：状态值更新 → 同 fact 新版本 + supersedes ───────


def test_value_update_new_version_same_fact(repo: Repository) -> None:
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    repo.finish_run(run, RunStatus.ACTIVE)

    fact = state_fact_id("e1", "cultivation_realm")
    v1 = _write_state(
        repo, run, fact, "cultivation_realm", "金丹",
        subject_entity_id="e1", chapter_id=ch, ordinal=1,
    )
    v2 = _write_state(
        repo, run, fact, "cultivation_realm", "元婴",
        subject_entity_id="e1", chapter_id=ch, ordinal=2,
    )

    assert v1.is_new and v2.is_new
    assert v1.claim_version_id != v2.claim_version_id
    cur = repo.current_version(fact)
    assert cur is not None
    assert cur["claim_version_id"] == v2.claim_version_id
    assert cur["supersedes_version_id"] == v1.claim_version_id


# ── 黄金场景 3：事实撤回 ───────────────────────────────────────


def test_retract_removes_from_current_view(repo: Repository) -> None:
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    repo.finish_run(run, RunStatus.ACTIVE)

    fact = relation_fact_id("e1", "师徒", "e2")
    v1 = _write_relation(repo, run, fact, "e1", "e2", "师徒", chapter_id=ch, ordinal=1)
    v2 = _write_relation(
        repo, run, fact, "e1", "e2", "师徒", chapter_id=ch, ordinal=3, op=Operation.RETRACT
    )
    assert v2.is_new and v2.claim_version_id != v1.claim_version_id
    # 版本链保留：v2.supersedes → v1
    row = repo.get_claim(v2.claim_version_id)
    assert row is not None and row["supersedes_version_id"] == v1.claim_version_id
    # 当前视图不再有该 fact（唯一版本已被撤回）
    assert repo.current_version(fact) is None


# ── 黄金场景 4：supports + refutes → contested ─────────────────


def test_aggregate_contested() -> None:
    assert aggregate_claim_status([]) == ClaimStatus.UNVERIFIED
    assert aggregate_claim_status([EvidenceStance.UNCLEAR]) == ClaimStatus.UNVERIFIED
    assert aggregate_claim_status([EvidenceStance.SUPPORTS]) == ClaimStatus.SUPPORTED
    assert (
        aggregate_claim_status([EvidenceStance.SUPPORTS, EvidenceStance.REFUTES])
        == ClaimStatus.CONTESTED
    )
    assert aggregate_claim_status([EvidenceStance.REFUTES]) == ClaimStatus.REJECTED


def test_evidence_write_and_contested(repo: Repository) -> None:
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    repo.finish_run(run, RunStatus.ACTIVE)

    fact = relation_fact_id("e1", "师徒", "e2")
    ver = _write_relation(repo, run, fact, "e1", "e2", "师徒", chapter_id=ch, ordinal=1)
    e1 = EvidenceRecord(
        evidence_id=evidence_id(ver.claim_version_id, ch, 10, 20, "h1"),
        claim_version_id=ver.claim_version_id,
        evidence_stance=EvidenceStance.SUPPORTS,
        chapter_id=ch,
        char_start=10,
        char_end=20,
        span_hash="h1",
    )
    e2 = EvidenceRecord(
        evidence_id=evidence_id(ver.claim_version_id, ch, 30, 40, "h2"),
        claim_version_id=ver.claim_version_id,
        evidence_stance=EvidenceStance.REFUTES,
        chapter_id=ch,
        char_start=30,
        char_end=40,
        span_hash="h2",
    )
    assert repo.write_evidence(e1)
    assert repo.write_evidence(e2)
    assert not repo.write_evidence(e1)  # 同 span 幂等

    rows = repo.evidence_for(ver.claim_version_id)
    stances = [EvidenceStance(r["evidence_stance"]) for r in rows]
    assert aggregate_claim_status(stances) == ClaimStatus.CONTESTED


# ── 黄金场景 5：实体合并审计 ──────────────────────────────────


def test_merge_audit(repo: Repository) -> None:
    book, _ = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    e1 = EntityRecord(canonical_id="ent_1", canonical_name="林风", created_by_run_id=run)
    e2 = EntityRecord(canonical_id="ent_2", canonical_name="林风", created_by_run_id=run)
    repo.upsert_entity(e1)
    repo.upsert_entity(e2)
    repo.record_merge("merge", "ent_1", "ent_2", run, reason="同名合并")

    audit = repo.merge_audit()
    assert len(audit) == 1
    assert audit[0]["action"] == "merge"
    assert audit[0]["from_entity_id"] == "ent_1"


# ── 黄金场景 6：同名实体不应误合并 ─────────────────────────────


def test_same_name_entities_not_merged(repo: Repository) -> None:
    book, _ = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    repo.upsert_entity(
        EntityRecord(canonical_id="ent_a", canonical_name="林风", created_by_run_id=run)
    )
    repo.upsert_entity(
        EntityRecord(canonical_id="ent_b", canonical_name="林风", created_by_run_id=run)
    )

    a = AliasClaim(
        alias_fact_id=alias_fact_id("ent_a", "林风"),
        claim_version_id="",
        canonical_id="ent_a",
        surface_name="林风",
        created_by_run_id=run,
        created_at=TS,
    )
    b = AliasClaim(
        alias_fact_id=alias_fact_id("ent_b", "林风"),
        claim_version_id="",
        canonical_id="ent_b",
        surface_name="林风",
        created_by_run_id=run,
        created_at=TS,
    )
    ra = repo.write_alias(a)
    rb = repo.write_alias(b)
    assert ra.is_new and rb.is_new
    assert ra.claim_version_id != rb.claim_version_id

    entities = repo.list_entities()
    assert len(entities) == 2
    assert {e["canonical_id"] for e in entities} == {"ent_a", "ent_b"}


# ── 黄金场景 7：多书隔离 ──────────────────────────────────────


def test_multi_book_isolation(repo: Repository) -> None:
    book1, ch1 = _seed_book_chapter(repo, book_id="book_1", chapter_id="ch_1")
    book2, ch2 = _seed_book_chapter(repo, book_id="book_2", chapter_id="ch_2")
    run1, run2 = new_uuid_id("run"), new_uuid_id("run")
    repo.start_run(run1, book1)
    repo.start_run(run2, book2)
    repo.finish_run(run1, RunStatus.ACTIVE)
    repo.finish_run(run2, RunStatus.ACTIVE)

    # 两本书各有同名人物「林风」的 alive 状态
    f1 = state_fact_id("ent_b1", "alive")
    f2 = state_fact_id("ent_b2", "alive")
    _write_state(
        repo, run1, f1, "alive", "true",
        subject_entity_id="ent_b1", chapter_id=ch1, ordinal=1,
    )
    _write_state(
        repo, run2, f2, "alive", "true",
        subject_entity_id="ent_b2", chapter_id=ch2, ordinal=1,
    )

    claims1 = repo.active_claims_for_book(book1)
    claims2 = repo.active_claims_for_book(book2)
    assert len(claims1) == 1
    assert len(claims2) == 1
    assert claims1[0]["fact_id"] == f1
    assert claims2[0]["fact_id"] == f2
    assert repo.list_active_runs(book1) == [run1]
    assert repo.list_active_runs(book2) == [run2]


# ── 黄金场景 8：failed run 不进默认查询 ────────────────────────


def test_failed_run_excluded_from_active_view(repo: Repository) -> None:
    book, ch = _seed_book_chapter(repo)
    run_ok, run_bad = new_uuid_id("run"), new_uuid_id("run")
    repo.start_run(run_ok, book)
    repo.start_run(run_bad, book)
    repo.finish_run(run_ok, RunStatus.ACTIVE)
    repo.finish_run(run_bad, RunStatus.FAILED, error="schema 校验失败")

    fact_ok = relation_fact_id("e1", "师徒", "e2")
    fact_bad = relation_fact_id("e3", "师徒", "e4")
    _write_relation(repo, run_ok, fact_ok, "e1", "e2", "师徒", chapter_id=ch, ordinal=1)
    _write_relation(repo, run_bad, fact_bad, "e3", "e4", "师徒", chapter_id=ch, ordinal=1)

    assert repo.current_version(fact_ok) is not None
    assert repo.current_version(fact_bad) is None
    # 数据可审计：claims 表里有 2 行
    with repo._engine.connect() as conn:  # noqa: SLF001
        assert conn.execute(text("SELECT count(*) FROM claims")).scalar() == 2


# ── 迁移与索引（02 验证项）────────────────────────────────────


def test_migrate_empty_to_head(tmp_path) -> None:
    db = tmp_path / "fresh.db"
    engine = _migrate(db)
    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        assert fk == []
        # 种子已写入
        assert conn.execute(text("SELECT count(*) FROM state_catalog")).scalar() == 6
        assert conn.execute(text("SELECT count(*) FROM ontology_versions")).scalar() == 2
    engine.dispose()


def test_migrate_roundtrip(tmp_path) -> None:
    """downgrade base → upgrade head 往返成功（备份副本升级路径）。"""
    db = tmp_path / "roundtrip.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = _migrate(db)  # 幂等（已在 head）
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM books")).scalar() == 0
    engine.dispose()


def _migrate(db) -> Engine:
    from novelcanon.storage.engine import create_db_engine

    engine = create_db_engine(db)
    migrate_to_head_cfg = Config("alembic.ini")
    migrate_to_head_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(migrate_to_head_cfg, "head")
    return engine


def test_explain_plan_uses_ordinal_index(repo: Repository) -> None:
    with repo._engine.connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            text("EXPLAIN QUERY PLAN SELECT * FROM claims WHERE observed_ordinal = 5")
        ).fetchall()
    plan = " ".join(str(r[3]) for r in rows)
    assert "ix_claims_observed_ordinal" in plan


def test_foreign_key_check_empty(repo: Repository) -> None:
    assert repo.foreign_key_check() == []


# ── 验收 P1：状态主体契约（update 语义 / 主体必填 / 实体引用）──


def test_state_update_without_prior_version_rejected(repo: Repository) -> None:
    """update 必须指向已存在版本：无旧版本直接拒绝，不得静默写成新事实。"""
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    _ensure_entity(repo, run, "e1")
    with pytest.raises(ValueError, match="必须指向已存在版本"):
        _write_state(
            repo, run, state_fact_id("e1", "realm"), "realm", "x",
            subject_entity_id="e1", chapter_id=ch, ordinal=1, op=Operation.UPDATE,
        )


def test_state_payload_requires_subject() -> None:
    """状态没有主体即非法（§5.4：subject_entity_id 必填）。"""
    with pytest.raises(ValidationError):
        StatePayload(field="cultivation_realm", value="金丹", raw_value="金丹")


def test_state_subject_must_exist_entity(repo: Repository) -> None:
    """状态主体必须引用已存在的实体（0005 触发器等价 FK）。"""
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    env = _envelope(
        run, state_fact_id("e_ghost", "realm"), chapter_id=ch, ordinal=1, claim_type="state"
    )
    with pytest.raises(Exception, match="引用不存在的实体"):  # noqa: B017
        repo.write_claim(
            env,
            StatePayload(
                field="realm", value="x", raw_value="x", subject_entity_id="e_ghost"
            ),
        )
    # PRAGMA foreign_key_check 依然无错（触发器约束不在 FK 检查范围）
    assert repo.foreign_key_check() == []


# ── 验收第二轮 P1：state_claims 真 NOT NULL + FK（0006 重建表）──


def test_state_claims_subject_not_null_at_db(repo: Repository) -> None:
    """直接 SQL 也不能写 NULL subject（NOT NULL 列约束）。"""
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    with pytest.raises(Exception), repo._engine.begin() as conn:  # noqa: B017, SLF001
        conn.execute(
            text(
                "INSERT INTO state_claims (claim_version_id, field, value, raw_value,"
                " subject_entity_id, target_entity_id)"
                " VALUES (:v, 'realm', 'x', 'x', NULL, NULL)"
            ),
            {"v": new_uuid_id("st")},
        )


def test_delete_entity_referenced_by_state_rejected(repo: Repository) -> None:
    """状态引用的实体不可删除（FK NO ACTION，foreign_keys=ON）。"""
    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    _write_state(
        repo, run, state_fact_id("e1", "realm"), "realm", "x",
        subject_entity_id="e1", chapter_id=ch, ordinal=1,
    )
    with pytest.raises(Exception), repo._engine.begin() as conn:  # noqa: B017, SLF001
        conn.execute(text("DELETE FROM entities WHERE canonical_id = 'e1'"))


# ── 验收第二轮 P1：事件查询默认事实过滤 ─────────────────────────


def _write_event(
    repo: Repository,
    run_id: str,
    fact_id: str,
    *,
    summary: str,
    chapter_id: str,
    ordinal: int,
    participants: list[str],
    entity_run: str,
) -> str:
    """写一个事件版本（含参与者）；返回 claim_version_id。"""
    for ent in participants:
        _ensure_entity(repo, entity_run, ent)
    env = _envelope(
        run_id, fact_id, chapter_id=chapter_id, ordinal=ordinal, claim_type="event"
    )
    result = repo.write_claim(
        env, EventPayload(event_type="测试事件", summary=summary)
    )
    for ent in participants:
        repo.add_event_participant(result.claim_version_id, ent)
    return result.claim_version_id


def test_event_participants_requires_current_supported(repo: Repository) -> None:
    """验收 P1：事件查询只返回 active run 中当前 supported 非 retract 版本。"""
    from novelcanon.query import QueryService

    book, ch = _seed_book_chapter(repo)
    run = new_uuid_id("run")
    repo.start_run(run, book)
    repo.finish_run(run, RunStatus.ACTIVE)

    fact = event_fact_id(
        "测试事件", ["e1", "e2"], None, ch, 1
    )
    v1 = _write_event(
        repo, run, fact, summary="初版描述", chapter_id=ch, ordinal=1,
        participants=["e1", "e2"], entity_run=run,
    )
    v2 = _write_event(
        repo, run, fact, summary="更新描述", chapter_id=ch, ordinal=2,
        participants=["e1", "e2"], entity_run=run,
    )
    repo.set_claim_status(v1, "supported")
    repo.set_claim_status(v2, "supported")

    q = QueryService(repo._engine, book)  # noqa: SLF001
    # 当前版本 v2 → 可查
    cur = q.event_participants(v2)
    assert cur is not None
    assert {p["entity_id"] for p in cur["participants"]} == {"e1", "e2"}
    # 历史版本 v1 → 拒绝（不是该 fact 当前版本）
    assert q.event_participants(v1) is None, "历史事件版本不得返回"

    # unverified 事件（不同 fact）→ 拒绝
    fact_uv = event_fact_id("测试事件", ["e3"], None, ch, 2)
    v_uv = _write_event(
        repo, run, fact_uv, summary="未验证", chapter_id=ch, ordinal=3,
        participants=["e3"], entity_run=run,
    )
    assert q.event_participants(v_uv) is None, "unverified 事件不得返回"
