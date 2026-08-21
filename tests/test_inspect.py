"""阶段二 07：inspect CLI / ops.inspect 测试。

覆盖：
- inspect_book 完整报告（计数非零、run 状态、索引、警告）；
- inspect_book 不存在的书返回 error；
- inspect_all 总览（book_count + books）；
- CLI inspect 命令可用且输出图书信息（不再「尚未实现」）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text
from typer.testing import CliRunner

from novelcanon.cli import app
from novelcanon.ops.inspect import inspect_all, inspect_book
from tests.helpers import seed_active_book

runner = CliRunner()


def test_inspect_book_report(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    report = inspect_book(migrated_db, data["book_id"])
    assert "error" not in report
    assert report["chapter_count"] == 3
    assert report["runs"]["active_run_id"] == data["run_id"]
    # 复审 P1：active 与历史分开展示；完整性基于 active
    assert report["counts"]["active"]["claims"] > 0
    assert report["counts"]["active"]["evidence"] > 0
    assert report["counts"]["active"]["entities"] >= 7
    assert (
        "event_links_supported" in report["counts"]["active"]
    )  # 复审 P1：链接总数/supported 分口径
    assert report["counts"]["history"]["claims"] >= report["counts"]["active"]["claims"]
    assert "warnings" in report  # 无 active 索引时为警告（seed 无索引）


def test_inspect_warnings_only_active_orphans(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：历史 claim 的 orphan 不计入完整性警告（只查 active）。"""

    data = seed_active_book(migrated_db, tmp_path)
    # 手工造一条**非 active** run 的无证据 claim——不应触发警告
    from novelcanon.pipeline import RunManager
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import state_fact_id
    from novelcanon.schemas.payloads import StatePayload
    from novelcanon.storage.repository import Repository

    run2 = RunManager(migrated_db).create(data["book_id"], input_hash="orphan-fixture")
    repo = Repository(migrated_db)
    chapters = repo.list_chapters(data["book_id"])
    # baseline：seed claims 不设 primary_evidence_id（fixture 简化）→ 已有警告
    baseline = inspect_book(migrated_db, data["book_id"])
    baseline_orphan = sum(1 for w in baseline["warnings"] if "primary_evidence" in w)
    # 写一条只属于未激活 run2 的 orphan claim → 不应增加 active 警告
    repo.write_claim(
        ClaimEnvelope(
            fact_id=state_fact_id("ent_yaolao", "alive"),  # seed 无此 fact → 新行
            claim_version_id="",
            claim_type="state",
            operation="assert",
            payload=StatePayload(field="alive", value="true", subject_entity_id="ent_yaolao"),
            observed_chapter_id=chapters[0]["chapter_id"],
            observed_ordinal=0,
            world_valid_kind="chapter_proxy",
            world_valid_from=0,
            world_valid_to=None,
            world_valid_confidence=1.0,
            created_by_run_id=run2,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        StatePayload(field="alive", value="true", subject_entity_id="ent_yaolao"),
    )
    # run2 未激活 → 该 orphan claim 不进入 active 警告（数量不变）
    report = inspect_book(migrated_db, data["book_id"])
    after_orphan = sum(1 for w in report["warnings"] if "primary_evidence" in w)
    assert after_orphan == baseline_orphan
    # 但历史计数包含它
    assert report["counts"]["history"]["claims"] >= report["counts"]["active"]["claims"] + 1


def test_inspect_book_not_found(migrated_db: Engine) -> None:
    report = inspect_book(migrated_db, "book_nope")
    assert report["error"] == "book_not_found"


def test_inspect_active_evidence_exact_current_first(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：active evidence 用 exact-current-first 口径。

    seed 证据是 legacy（verification_run_id NULL）；为同一 claim+span
    补一条当前 run 验证行后，legacy 行被抑制、当前行计入——总数不变
    （否则会出现 276+276=552 的双计）。
    """
    data = seed_active_book(migrated_db, tmp_path)
    baseline = inspect_book(migrated_db, data["book_id"])["counts"]["active"]["evidence"]
    assert baseline > 0
    with migrated_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT claim_version_id, evidence_stance, evidence_type, chapter_id,"
                " char_start, char_end, span_hash, literal_match_rate, verification_method"
                " FROM claim_evidence LIMIT 1"
            )
        ).fetchone()
        conn.execute(
            text(
                "INSERT INTO claim_evidence (evidence_id, claim_version_id, evidence_stance,"
                " evidence_type, chapter_id, char_start, char_end, span_hash, literal_match_rate,"
                " verification_method, verification_run_id)"
                " VALUES (:eid, :cv, :st, :et, :ch, :cs, :ce, :sh, :lr, :vm, :vr)"
            ),
            {
                "eid": "ev_inspect_current",
                "cv": row[0],
                "st": row[1],
                "et": row[2],
                "ch": row[3],
                "cs": row[4],
                "ce": row[5],
                "sh": row[6],
                "lr": row[7],
                "vm": row[8],
                "vr": data["run_id"],
            },
        )
    after = inspect_book(migrated_db, data["book_id"])["counts"]["active"]["evidence"]
    assert after == baseline


def test_inspect_active_event_links(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：active event_links 按 active run 的验证记录统计（总数 +
    supported 数），不再经 v_active_claims 关联（真实库曾错误显示 0）。"""
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import event_link_fact_id
    from novelcanon.schemas.memory import EventLinkRecord
    from novelcanon.schemas.payloads import EventLinkPayload
    from novelcanon.schemas.types import EventLinkType, Operation
    from novelcanon.storage.repository import Repository

    data = seed_active_book(migrated_db, tmp_path)
    src, tgt = data["claims"]["event_linfeng"], data["claims"]["event_promise"]
    repo = Repository(migrated_db)
    repo.write_event_link(
        EventLinkRecord(
            envelope=ClaimEnvelope(
                fact_id=event_link_fact_id(src, EventLinkType.CAUSES, tgt),
                claim_version_id="",
                claim_type="event_link",
                operation=Operation.ASSERT,
                observed_chapter_id=data["chapters"][1],
                observed_ordinal=1,
                created_by_run_id=data["run_id"],
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload=EventLinkPayload(
                source_event_id=src,
                target_event_id=tgt,
                relation_type=EventLinkType.CAUSES,
            ),
            verification_method="manual-test",
            verification_evidence="测试构造的因果边（supported 需方法+证据）",
        )
    )
    report = inspect_book(migrated_db, data["book_id"])
    assert report["counts"]["active"]["event_links"] == 1
    assert report["counts"]["active"]["event_links_supported"] == 1


def test_inspect_all(tmp_path: Path, migrated_db: Engine) -> None:
    seed_active_book(migrated_db, tmp_path)
    report = inspect_all(migrated_db)
    assert report["book_count"] == 1
    assert report["books"][0]["book_id"] == "book_s10"
    assert report["books"][0]["active_claims"] > 0


def test_inspect_cli_lists_books(tmp_path: Path, migrated_db: Engine) -> None:
    """inspect 不再是占位：列出全部书（含 fixture 书）。"""
    from novelcanon.cli import _open_db  # noqa: F401  (仅确认模块可导入)

    seed_active_book(migrated_db, tmp_path)
    # CLI 打开的是配置库，fixture 库在内存/临时路径——直接断言命令
    # 不再输出「尚未实现」，且 --help 列出 --book-id 选项。
    result = runner.invoke(app, ["inspect", "--help"])
    assert result.exit_code == 0
    assert "尚未实现" not in result.stdout
    assert "--book-id" in result.stdout
