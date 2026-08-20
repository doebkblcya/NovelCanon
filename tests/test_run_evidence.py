"""阶段 11 复审（十六轮）P1×4 闭环测试。

- P1-1 exact-current-first：QueryService 证据查询 / 章节引用不再返回
  legacy + current 两套重复（统一 evidence/selector 入口）；
- P1-2 EventLinkService 证据读取按 run 隔离：link primary 必须属于
  当前 run（legacy 同 span 并存时优先 current）；
- P1-3 Validator 激活门禁拒绝「只有旧 run evidence」的 supported claim；
- P1-4 0017 迁移恢复枚举 CHECK 与 ON DELETE CASCADE（upgrade/downgrade 双向）。
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from novelcanon.events.service import EventLinkService
from novelcanon.evidence.service import EvidenceService
from novelcanon.pipeline.run import RunManager
from novelcanon.pipeline.validation import Activator, Validator
from novelcanon.query.service import QueryService
from novelcanon.schemas.types import RunStatus
from tests.test_events import _book_and_chapters, _seed_events
from tests.test_evidence import _book_and_chapter, build_real_draft

ALEMBIC_INI = "alembic.ini"


def _activate(engine: Engine, run_id: str) -> None:
    mgr = RunManager(engine)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(engine).activate(run_id) is None


# ── P1-1：QueryService exact-current-first ─────────────────────


def test_query_evidence_exact_current_first(tmp_path, migrated_db: Engine) -> None:
    """同 claim/span 存在 legacy NULL + current 两套时，查询只返回 current；
    删除 current 后回退 legacy（不返回空、不重复）。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="run-aware")
    service = EvidenceService(migrated_db)
    stats = service.align_chapter(
        run_id,
        book_id,
        build_real_draft(chapter_id, chapter_text),
        chapter_text,
        "draft_1",
    )
    assert stats.errors == []
    _activate(migrated_db, run_id)

    with migrated_db.connect() as conn:
        vid = conn.execute(
            text("SELECT claim_version_id FROM claims WHERE created_by_run_id = :r LIMIT 1"),
            {"r": run_id},
        ).scalar()
        cur = conn.execute(
            text(
                "SELECT evidence_id, chapter_id, char_start, char_end, span_hash"
                " FROM claim_evidence WHERE claim_version_id = :v AND verification_run_id = :r"
            ),
            {"v": vid, "r": run_id},
        ).fetchone()
    assert cur is not None, "align 应产生当前 run 证据"

    # 同 span 插入 legacy NULL evidence（不同 evidence_id，模拟 run 机制前写入）
    legacy_id = "ev_legacy_" + cur[0]
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                " evidence_type, chapter_id, char_start, char_end, span_hash,"
                " literal_match_rate, verification_method, verification_run_id)"
                " VALUES (:eid, :v, 'supports', 'direct', :ch, :cs, :ce, :sh, 1.0,"
                " 'legacy', NULL)"
            ),
            {
                "eid": legacy_id,
                "v": vid,
                "ch": cur[1],
                "cs": cur[2],
                "ce": cur[3],
                "sh": cur[4],
            },
        )

    q = QueryService(migrated_db, book_id)
    evs = q._evidence_for(vid)
    assert len(evs) == 1, f"exact-current-first 只返回 current：{evs}"
    assert evs[0]["evidence_id"] == cur[0]
    cite = q.chapter_citation(vid)
    assert cite is not None and cite["span_hash"] == cur[4], "citation 应稳定取 current span"

    # 删除 current → 回退 legacy（不返回空）
    with migrated_db.begin() as conn:
        conn.execute(
            text("DELETE FROM claim_evidence WHERE verification_run_id = :r"), {"r": run_id}
        )
    evs = q._evidence_for(vid)
    assert len(evs) == 1 and evs[0]["evidence_id"] == legacy_id, "无 current 时回退 legacy"


# ── P1-2：EventLinkService 证据按 run 隔离 ─────────────────────


def test_event_link_evidence_scoped_to_run(tmp_path, migrated_db: Engine) -> None:
    """事件证据读取按 run 隔离；落库后所有 link primary 属于当前 run。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _activate(migrated_db, run_id)

    # 给每个事件补一条 current run 的验证证据（legacy NULL 并存 → 优先 current）
    with migrated_db.begin() as conn:
        for vid in version_ids:
            row = conn.execute(
                text(
                    "SELECT chapter_id, char_start, char_end, span_hash FROM claim_evidence"
                    " WHERE claim_version_id = :v LIMIT 1"
                ),
                {"v": vid},
            ).fetchone()
            conn.execute(
                text(
                    "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                    " evidence_type, chapter_id, char_start, char_end, span_hash,"
                    " literal_match_rate, verification_method, verification_run_id)"
                    " VALUES (:eid, :v, 'supports', 'direct', :ch, :cs, :ce, :sh, 1.0,"
                    " 'hash-exact/v2', :r)"
                ),
                {
                    "eid": "ev_cur_" + vid,
                    "v": vid,
                    "ch": row[0],
                    "cs": row[1],
                    "ce": row[2],
                    "sh": row[3],
                    "r": run_id,
                },
            )

    svc = EventLinkService(migrated_db)
    stances = svc._evidence_stances(version_ids[0], run_id)
    assert len(stances) == 1, f"stance 读取应按 run 隔离（同 span 只取 current）：{stances}"

    stats = svc.link_run(run_id, book_id)
    assert stats.links > 0
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT l.primary_evidence_id, ce.verification_run_id FROM event_links l"
                " JOIN claim_evidence ce ON ce.evidence_id = l.primary_evidence_id"
            )
        ).fetchall()
    assert rows, "link 应有 primary evidence"
    for _eid, vr in rows:
        assert vr == run_id, f"link primary 必须属于当前 run：{_eid} -> {vr}"

    # 幂等重跑：primary 必须被更新为本 run 证据（16 轮：不残留历史 run 锚定）
    stats2 = svc.link_run(run_id, book_id)
    assert stats2.links == stats.links
    with migrated_db.connect() as conn:
        rows2 = conn.execute(
            text(
                "SELECT l.primary_evidence_id, ce.verification_run_id FROM event_links l"
                " JOIN claim_evidence ce ON ce.evidence_id = l.primary_evidence_id"
            )
        ).fetchall()
    assert len(rows2) == len(rows)
    for _eid, vr in rows2:
        assert vr == run_id, f"重跑后 link primary 仍须属于当前 run：{_eid} -> {vr}"


# ── P1-3：Validator 激活门禁拒绝旧 run evidence ────────────────


def test_validator_rejects_old_run_evidence(tmp_path, migrated_db: Engine) -> None:
    """supported claim 只有旧 run evidence（primary 指向旧 run）时必须拦截；
    primary 更新为本 run evidence 后通过。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    old_run = RunManager(migrated_db).create(book_id, input_hash="old-run")
    service = EvidenceService(migrated_db)
    stats = service.align_chapter(
        old_run,
        book_id,
        build_real_draft(chapter_id, chapter_text),
        chapter_text,
        "draft_1",
    )
    assert stats.errors == []

    # 新 run：把 old_run 的 claim 关联进来（模拟 Map 复用误配置带入旧 claim）
    new_run = RunManager(migrated_db).create(book_id, input_hash="new-run")
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO claim_observations (claim_version_id, extraction_run_id,"
                " observed_at) SELECT claim_version_id, :r2, '2026-01-01T00:00:00+00:00'"
                " FROM claim_observations WHERE extraction_run_id = :r1"
            ),
            {"r2": new_run, "r1": old_run},
        )

    validator = Validator(migrated_db)
    issues = validator.issues(new_run, total_chapters=0)
    assert any("supported 但无有效 primary evidence" in i for i in issues), issues

    # 补 new_run 证据（同 span）→ primary 仍指向旧 run → 继续拦截
    with migrated_db.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT c.claim_version_id, ce.chapter_id, ce.char_start, ce.char_end,"
                " ce.span_hash FROM claims c JOIN claim_evidence ce"
                " ON ce.claim_version_id = c.claim_version_id"
                " WHERE c.created_by_run_id = :r ORDER BY c.rowid"
            ),
            {"r": old_run},
        ).fetchall()
        for vid, ch, cs, ce, sh in rows:
            conn.execute(
                text(
                    "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                    " evidence_type, chapter_id, char_start, char_end, span_hash,"
                    " literal_match_rate, verification_method, verification_run_id)"
                    " VALUES ('ev_new_' || :v, :v, 'supports', 'direct', :ch, :cs, :ce, :sh,"
                    " 1.0, 'hash-exact/v2', :r)"
                ),
                {"v": vid, "ch": ch, "cs": cs, "ce": ce, "sh": sh, "r": new_run},
            )
    issues = validator.issues(new_run, total_chapters=0)
    assert any("supported 但无有效 primary evidence" in i for i in issues), (
        "primary 属于旧 run 仍应拦截"
    )

    # primary 指向 new_run 证据 → 通过
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE claims SET primary_evidence_id = 'ev_new_' || claim_version_id"
                " WHERE created_by_run_id = :r"
            ),
            {"r": old_run},
        )
    issues = validator.issues(new_run, total_chapters=0)
    assert issues == [], issues


# ── P1-4：0017 迁移恢复约束（upgrade/downgrade 双向）────────────


def _insert_claim_evidence(
    engine: Engine,
    chapter_id: str,
    run_id: str,
    vid: str,
    *,
    stance: str = "supports",
    etype: str = "direct",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO claims (fact_id, claim_version_id, claim_type, operation,"
                " claim_status, observed_chapter_id, observed_ordinal, created_by_run_id,"
                " created_at) VALUES ('fact_' || :vid, :vid, 'state', 'assert', 'supported',"
                " :ch, 1, :r, '2026-01-01T00:00:00+00:00')"
            ),
            {"vid": vid, "ch": chapter_id, "r": run_id},
        )
        conn.execute(
            text(
                "INSERT INTO claim_observations (claim_version_id, extraction_run_id,"
                " observed_at) VALUES (:vid, :r, '2026-01-01T00:00:00+00:00')"
            ),
            {"vid": vid, "r": run_id},
        )
        conn.execute(
            text(
                "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                " evidence_type, chapter_id, char_start, char_end, span_hash,"
                " literal_match_rate, verification_method, verification_run_id)"
                " VALUES ('ev_' || :vid, :vid, :st, :et, :ch, 0, 3, 'abc', 1.0,"
                " 'hash-exact', :r)"
            ),
            {"vid": vid, "ch": chapter_id, "r": run_id, "st": stance, "et": etype},
        )


def _assert_schema_constraints(engine: Engine, chapter_id: str, run_id: str, tag: str) -> None:
    """非法枚举拒绝 + 删除 claim 级联清理（upgrade/downgrade 后均须成立）。"""
    _insert_claim_evidence(engine, chapter_id, run_id, f"ver_{tag}")
    with pytest.raises(IntegrityError):
        _insert_claim_evidence(engine, chapter_id, run_id, f"bad_stance_{tag}", stance="nonsense")
    with pytest.raises(IntegrityError):
        _insert_claim_evidence(engine, chapter_id, run_id, f"bad_type_{tag}", etype="nonsense")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM claims WHERE claim_version_id = :v"), {"v": f"ver_{tag}"})
    with engine.connect() as conn:
        n_ev = conn.execute(
            text("SELECT COUNT(*) FROM claim_evidence WHERE claim_version_id = :v"),
            {"v": f"ver_{tag}"},
        ).scalar()
        n_obs = conn.execute(
            text("SELECT COUNT(*) FROM claim_observations WHERE claim_version_id = :v"),
            {"v": f"ver_{tag}"},
        ).scalar()
    assert n_ev == 0, "删除 claim 必须级联删除 evidence"
    assert n_obs == 0, "删除 claim 必须级联删除 observation"


def _alembic_version(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_0017_restores_schema_constraints(tmp_path, migrated_db: Engine) -> None:
    """0017/0018 重建表恢复枚举 CHECK 与 ON DELETE CASCADE；downgrade 还原
    v1 表同样保留原约束（upgrade/downgrade 双向验证，每步断言版本号）。

    十六轮 P1：旧实现用 command.upgrade("0016_runs_abandoned")——库已在
    0017 时对祖先版本 upgrade 是 no-op，根本没执行 downgrade；必须用
    command.downgrade 并核对 alembic_version 变化。
    """
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="mig-0017")
    db_path = migrated_db.url.database

    assert _alembic_version(migrated_db) == "0018_restore_evidence_constraints"
    _assert_schema_constraints(migrated_db, chapter_id, run_id, "head")

    def _migrate(revision: str, *, downgrade: bool) -> None:
        cfg = Config(ALEMBIC_INI)
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        if downgrade:
            command.downgrade(cfg, revision)
        else:
            command.upgrade(cfg, revision)

    # 真正 downgrade 到 0016（经 0018.downgrade → 0017.downgrade）
    _migrate("0016_runs_abandoned", downgrade=True)
    assert _alembic_version(migrated_db) == "0016_runs_abandoned", "downgrade 必须实际执行"
    _assert_schema_constraints(migrated_db, chapter_id, run_id, "v1")

    # 回到 head（经 0017.upgrade → 0018.upgrade）
    _migrate("head", downgrade=False)
    assert _alembic_version(migrated_db) == "0018_restore_evidence_constraints"
    _assert_schema_constraints(migrated_db, chapter_id, run_id, "head2")


def test_0018_preserves_multiple_verifications(tmp_path, migrated_db: Engine) -> None:
    """0018 原样迁移全部证据行：同 span 多验证（legacy + current run）不按
    span 去重——0017 语义下 v1/v2 与跨 run 历史必须完整保留（downgrade 到
    0017 再 upgrade 重放后仍在）。"""
    book_id, chapter_id, chapter_text = _book_and_chapter(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="mig-0018-keep")
    db_path = migrated_db.url.database
    vid = "ver_multi"
    with migrated_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO claims (fact_id, claim_version_id, claim_type, operation,"
                " claim_status, observed_chapter_id, observed_ordinal, created_by_run_id,"
                " created_at) VALUES ('fact_multi', :v, 'state', 'assert', 'supported',"
                " :ch, 1, :r, '2026-01-01T00:00:00+00:00')"
            ),
            {"v": vid, "ch": chapter_id, "r": run_id},
        )
        conn.execute(
            text(
                "INSERT INTO claim_observations (claim_version_id, extraction_run_id,"
                " observed_at) VALUES (:v, :r, '2026-01-01T00:00:00+00:00')"
            ),
            {"v": vid, "r": run_id},
        )
        # 同 span 两条：legacy NULL + current run（0017 并存语义）
        for eid, run in (("ev_a", None), ("ev_b", run_id)):
            conn.execute(
                text(
                    "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                    " evidence_type, chapter_id, char_start, char_end, span_hash,"
                    " literal_match_rate, verification_method, verification_run_id)"
                    " VALUES (:eid, :v, 'supports', 'direct', :ch, 0, 3, 'abc', 1.0,"
                    " 'hash-exact', :r)"
                ),
                {"eid": eid, "v": vid, "ch": chapter_id, "r": run},
            )

    # downgrade 到 0017（只回退 0018）→ upgrade head（0018 重放）
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(cfg, "0017_evidence_run_version")
    assert _alembic_version(migrated_db) == "0017_evidence_run_version"
    command.upgrade(cfg, "head")
    assert _alembic_version(migrated_db) == "0018_restore_evidence_constraints"
    with migrated_db.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM claim_evidence WHERE claim_version_id = :v"),
            {"v": vid},
        ).scalar()
    assert n == 2, f"0018 不得按 span 去重（同 span 多验证须并存）：{n}"
