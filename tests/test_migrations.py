"""迁移测试：0013 回填与 0012 约束（验收 P0/P1）。

覆盖：
- 从 0012 带多 run 复用数据升级到 head：event_link_verifications 必须为
  每个 (link, run) 观察关系回填验证行——只按 created_by_run_id 回填会让
  复用边的 active run 升级后丢失可见性；
- 0012 world-valid 列约束（NOT NULL kind / 枚举 / confidence / 组合）。
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

ALEMBIC_INI = "alembic.ini"


def _upgrade(db_path: Path, revision: str) -> None:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, revision)


def _seed_multi_run_links(tmp_path, engine: Engine) -> tuple[str, str]:
    """在 0012 版本库中种子：书 + run1（创建边）+ run2（复用同一边），
    返回 (book_id, edge_claim_version_id)。"""
    from novelcanon.ingestion.service import import_book
    from novelcanon.pipeline import RunManager
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.payloads import EventPayload
    from novelcanon.schemas.types import ClaimStatus, EntityTier, Operation
    from novelcanon.storage.repository import Repository
    from tests.helpers import make_fixture_epub

    epub = tmp_path / "mig.epub"
    make_fixture_epub(
        epub,
        [("第一章", "陆尘拜入青云宗。"), ("第二章", "陆尘闭关突破。")],
        title="迁移",
    )
    book_id = import_book(engine, epub).book_id
    repo = Repository(engine)
    chs = repo.list_chapters(book_id)
    run1 = RunManager(engine).create(book_id, input_hash="r1")
    repo.upsert_entity(
        EntityRecord(
            canonical_id="ent_luchen", canonical_name="陆尘",
            tier=EntityTier.CORE, created_by_run_id=run1,
        )
    )
    # 两个事件 claim（source/target）
    from novelcanon.schemas.ids import event_fact_id, event_link_fact_id

    payloads = [
        EventPayload(event_type="拜师", summary="陆尘拜入青云宗",
                     location_entity_id=None, sequence_in_chapter=1),
        EventPayload(event_type="突破", summary="陆尘闭关突破",
                     location_entity_id=None, sequence_in_chapter=1),
    ]
    vids = []
    for ordinal, p in enumerate(payloads):
        fact = event_fact_id(
            p.event_type, ["ent_luchen"], None, chs[ordinal]["chapter_id"], 1
        )
        wr = repo.write_claim(
            ClaimEnvelope(
                fact_id=fact, claim_version_id="", claim_type="event",
                operation=Operation.ASSERT, claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=chs[ordinal]["chapter_id"],
                observed_ordinal=ordinal, created_by_run_id=run1,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            p,
        )
        vids.append(wr.claim_version_id)
    # run1 创建边（supported，带验证信息）
    from novelcanon.schemas.types import EventLinkType

    edge_fact = event_link_fact_id(vids[0], EventLinkType.CAUSES, vids[1])
    edge_id = f"edge_{edge_fact[:20]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_links (claim_version_id, fact_id,"
                " source_event_id, target_event_id, relation_type, confidence,"
                " claim_status, observed_chapter_id, observed_ordinal,"
                " created_by_run_id, world_valid_kind, world_valid_from,"
                " world_valid_confidence, verification_method,"
                " verification_evidence)"
                " VALUES (:v, :f, :s, :t, 'causes', 0.8, 'supported',"
                " :ch, 1, :run, 'chapter_proxy', 1, 1.0, 'causal-connective',"
                " '拜师之后突破（迁移种子）')"
            ),
            {
                "v": edge_id, "f": edge_fact, "s": vids[0], "t": vids[1],
                "ch": chs[1]["chapter_id"], "run": run1,
            },
        )
    # 第二条边：claim_status=supported 但方法/证据是空字符串/纯空白
    # （P0：回填必须把空字符串视为缺失 → 降级 unverified）
    from novelcanon.schemas.types import EventLinkType as _ELT

    edge2_fact = event_link_fact_id(vids[1], _ELT.CAUSES, vids[0])
    edge2_id = f"edge2_{edge2_fact[:20]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_links (claim_version_id, fact_id,"
                " source_event_id, target_event_id, relation_type, confidence,"
                " claim_status, observed_chapter_id, observed_ordinal,"
                " created_by_run_id, world_valid_kind, world_valid_from,"
                " world_valid_confidence, verification_method,"
                " verification_evidence)"
                " VALUES (:v, :f, :s, :t, 'causes', 0.8, 'supported',"
                " :ch, 1, :run, 'chapter_proxy', 1, 1.0, '', '  ')"
            ),
            {
                "v": edge2_id, "f": edge2_fact, "s": vids[1], "t": vids[0],
                "ch": chs[0]["chapter_id"], "run": run1,
            },
        )
    # run2 复用两条边（成员关系 = observations）
    run2 = RunManager(engine).create(book_id, input_hash="r2")
    with engine.begin() as conn:
        for eid in (edge_id, edge2_id):
            for run in (run1, run2):
                conn.execute(
                    text(
                        "INSERT INTO event_link_observations (claim_version_id,"
                        " extraction_run_id, observed_at) VALUES (:v, :run, :ts)"
                    ),
                    {"v": eid, "run": run, "ts": "2026-01-01T00:00:00+00:00"},
                )
    # run2 是当前 active run（复用者）——升级后它必须有验证行
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE extraction_runs SET status = 'active' WHERE run_id = :r"),
            {"r": run2},
        )
    return book_id, edge_id, edge2_id, vids[0]


def test_0013_backfill_from_observations(tmp_path) -> None:
    """验收 P0：从 0012 带多 run 数据升级到 head，验证行按
    event_link_observations（成员关系）回填每个 (link, run)——复用边的
    active run 升级后不丢可见性。"""
    from novelcanon.storage.engine import create_db_engine

    db = tmp_path / "mig0013.db"
    engine = create_db_engine(db)
    _upgrade(db, "0012_event_link_world_valid")
    book_id, edge_id, edge2_id, src_event = _seed_multi_run_links(tmp_path, engine)

    _upgrade(db, "head")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT extraction_run_id, claim_status, verification_method"
                " FROM event_link_verifications WHERE claim_version_id = :e"
                " ORDER BY extraction_run_id"
            ),
            {"e": edge_id},
        ).fetchall()
    # 两个 run（创建者 + 复用者）都必须有验证行
    assert len(rows) == 2, f"每个 (link, run) 观察关系都必须回填：{rows}"
    assert all(r[1] == "supported" for r in rows), (
        f"回填状态 = 全局 supported（带方法/证据）：{rows}"
    )
    assert all(r[2] == "causal-connective" for r in rows)

    # 空字符串/纯空白证据的 supported 存量 → 回填降级为 unverified
    with engine.connect() as conn:
        rows2 = conn.execute(
            text(
                "SELECT extraction_run_id, claim_status, verification_method"
                " FROM event_link_verifications WHERE claim_version_id = :e"
                " ORDER BY extraction_run_id"
            ),
            {"e": edge2_id},
        ).fetchall()
    assert len(rows2) == 2, f"edge2 也应回填两个 run：{rows2}"
    assert all(r[1] == "unverified" and r[2] is None for r in rows2), (
        f"空字符串证据必须降级为 unverified：{rows2}"
    )

    # active run（run2，复用者）升级后因果查询仍可见（INNER JOIN 不丢边）
    from novelcanon.query import QueryService

    q = QueryService(engine, book_id)
    paths = q.causal_paths(src_event)
    assert paths, "复用边的 active run 升级后必须仍可见"


# ── 阶段 10：0014 查询缓存 / 分层摘要 / 卷分组来源 ─────────────


def test_0014_query_cache_and_summaries_schema(tmp_path) -> None:
    """0014 建表：query_cache 与 summary_artifacts 存在且带关键约束。"""
    from novelcanon.storage.engine import create_db_engine

    db = tmp_path / "mig0014.db"
    engine = create_db_engine(db)
    _upgrade(db, "head")
    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "query_cache" in tables
    assert "summary_artifacts" in tables
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(summary_artifacts)"))}
    for required in (
        "summary_id",
        "level",
        "input_claim_versions",
        "depends_on_summaries",
        "content_hash",
        "max_observed_ordinal",
        "status",
        "grouping_version",
    ):
        assert required in cols, f"summary_artifacts 缺列 {required}"
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(query_cache)"))}
    for required in (
        "cache_key",
        "active_run_signature",
        "index_version_id",
        "knowledge_cutoff",
        "world_at",
    ):
        assert required in cols, f"query_cache 缺列 {required}"
    # volumes 补 grouping_source 列
    with engine.connect() as conn:
        vcols = {r[1] for r in conn.execute(text("PRAGMA table_info(volumes)"))}
    assert "grouping_source" in vcols
    engine.dispose()


def test_0014_summary_status_check(tmp_path) -> None:
    """summary_artifacts 的 level/status 枚举约束生效。"""
    from novelcanon.storage.engine import create_db_engine

    db = tmp_path / "mig0014b.db"
    engine = create_db_engine(db)
    _upgrade(db, "head")
    with engine.connect() as conn:
        bad = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='x'")
        ).fetchall()
    assert bad == []
    with engine.begin() as conn:
        try:
            conn.execute(
                text(
                    "INSERT INTO summary_artifacts (summary_id, book_id, level,"
                    " content_hash, max_observed_ordinal, status, created_at)"
                    " VALUES ('s1', 'b1', 'bad_level', 'h', 1, 'valid', 'ts')"
                )
            )
            raise AssertionError("非法 level 应被 CHECK 拒绝")
        except sqlalchemy.exc.IntegrityError:
            pass
    engine.dispose()
