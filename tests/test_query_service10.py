"""阶段 10 查询服务扩展测试（docs/implementation/10 §路线表）。

覆盖验证项：
- 实体状态/一跳关系/势力成员/术语释义/按章图谱/实体快照均带证据返回；
- 双时间过滤（cutoff/world_at）在各路线一致；
- org 日志折叠（leave 后成员消失）；
- 结构化查询只返回 supported 当前版本。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from novelcanon.query import QueryService
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import org_fact_id
from novelcanon.schemas.payloads import OrgPayload
from novelcanon.schemas.types import ClaimStatus, Operation
from novelcanon.storage.repository import Repository
from tests.helpers import seed_active_book


def test_term_definition_route(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    qs = QueryService(migrated_db, data["book_id"])
    d = qs.term_definition("异火")
    assert d is not None
    assert "天地奇物" in d["definition"]
    assert d["observed_ordinal"] == 2
    assert d["evidence"]  # 带证据
    assert qs.term_definition("不存在的术语") is None


def test_org_membership_folding(tmp_path: Path, migrated_db: Engine) -> None:
    """org 日志折叠：join 保留、leave 移除、role 取最新。"""
    data = seed_active_book(migrated_db, tmp_path)
    qs = QueryService(migrated_db, data["book_id"])
    members = qs.org_membership("ent_xiaoyan")
    assert [(m["org_entity_id"], m["role"]) for m in members] == [("ent_xiaojia", "少主")]
    # 林风加入青云宗（弟子）已在 seed
    members_lin = qs.org_membership("ent_linfeng")
    assert [(m["org_entity_id"], m["role"]) for m in members_lin] == [("ent_qingyunzong", "弟子")]
    # leave 后成员消失（折叠）
    repo = Repository(migrated_db)
    repo.write_claim(
        ClaimEnvelope(
            fact_id=org_fact_id("ent_xiaojia", "ent_xiaoyan", "少主"),
            claim_version_id="",
            claim_type="org",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][2],
            observed_ordinal=2,
            world_valid_kind="chapter_proxy",
            world_valid_from=2,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        OrgPayload(
            org_entity_id="ent_xiaojia",
            member_entity_id="ent_xiaoyan",
            role="少主",
            action="leave",
        ),
    )
    assert qs.org_membership("ent_xiaoyan") == []


def test_chapter_graph_dual_time(tmp_path: Path, migrated_db: Engine) -> None:
    """按章图谱：cutoff 与 world_at 独立过滤。"""
    data = seed_active_book(migrated_db, tmp_path)
    qs = QueryService(migrated_db, data["book_id"])
    g = qs.chapter_graph(1)
    types = {c["claim_type"] for c in g}
    assert {"state", "relation", "event", "org"} <= types
    for c in g:
        assert c["evidence"] is not None
    # cutoff=0 → ch2（ordinal 1）不可见
    g0 = qs.chapter_graph(1, knowledge_cutoff=0)
    assert g0 == []
    # world_at=0 → ch2 的 chapter_proxy(from=1) 不覆盖
    gw = qs.chapter_graph(1, world_at=0)
    assert gw == []


def test_entity_snapshot_unified(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    qs = QueryService(migrated_db, data["book_id"])
    snap = qs.entity_snapshot("ent_xiaoyan")
    assert snap["display_name"] == "萧炎"
    assert snap["state"][0]["field"] == "alive"
    assert [r["relation_type"] for r in snap["relations"]] == ["师徒", "恋人"]
    assert snap["org_membership"][0]["role"] == "少主"
    assert [e["event_type"] for e in snap["events"]] == ["定约"]
    # 每个条目带证据与状态
    for r in snap["relations"]:
        assert r["claim_status"] == "supported"
        assert r["evidence"]


def test_dual_time_consistent_across_routes(tmp_path: Path, migrated_db: Engine) -> None:
    """多书/cutoff/world 过滤在所有路线一致（10 验证项）。"""
    data = seed_active_book(migrated_db, tmp_path)
    qs = QueryService(migrated_db, data["book_id"])
    # world_at=1 时：ch1 披露（ordinal 0，from=0）与 ch2 披露（ordinal 1，from=1）都可见
    rel = qs.one_hop_relations("ent_xiaoyan", knowledge_cutoff=1, world_at=1)
    assert len(rel) == 2
    # world_at=0 时：ch2 的关系（from=1）不可见
    rel0 = qs.one_hop_relations("ent_xiaoyan", knowledge_cutoff=5, world_at=0)
    assert rel0 == []
    # cutoff=0 时：即使 world_at 覆盖也不可见（读者未披露）
    relc = qs.one_hop_relations("ent_xiaoyan", knowledge_cutoff=0, world_at=1)
    assert relc == []


def test_unknown_world_visible_in_plain_query_hidden_in_world_query(
    tmp_path: Path, migrated_db: Engine
) -> None:
    """unknown 世界时间：普通查询（无 world_at）可见；世界时间查询排除。

    阶段 07 及更早写入的历史 claim 无 world 元数据（默认 unknown）——
    双时间查询引入后不得让它们从默认查询消失（阶段 10 验收 P0）。
    """
    data = seed_active_book(migrated_db, tmp_path)
    repo = Repository(migrated_db)
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.payloads import RelationPayload

    # 写入一条 world_valid_kind 缺省（unknown）的关系（新 fact：盟友）
    repo.write_claim(
        ClaimEnvelope(
            fact_id=relation_fact_id("ent_yaolao", "盟友", "ent_xiaoyan"),
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][1],
            observed_ordinal=1,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_yaolao",
            to_entity_id="ent_xiaoyan",
            relation_type="盟友",
            relation_raw="药老与萧炎结盟",
        ),
    )
    qs = QueryService(migrated_db, data["book_id"])
    plain = qs.one_hop_relations("ent_xiaoyan")
    assert any(r["relation_type"] == "盟友" for r in plain), (
        "普通查询（无 world_at）应返回 unknown 世界时间的关系"
    )
    world = qs.one_hop_relations("ent_xiaoyan", world_at=2)
    assert all(r["relation_type"] != "盟友" for r in world), (
        "世界时间查询必须排除 unknown 世界时间（不能表达为精确状态）"
    )


def test_claim_history_limited_to_active_runs(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（复审）：claim_history 经 observation 限定 active run——
    失败/失效 run 的版本不计入。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.pipeline import RunManager
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.payloads import RelationPayload
    from novelcanon.schemas.types import RunStatus

    fact = relation_fact_id("ent_yaolao", "师徒", "ent_xiaoyan")
    # 第二个 run：写入第 2 版后标记 failed（不激活）
    repo = Repository(migrated_db)
    mgr = RunManager(migrated_db)
    run2 = mgr.create(data["book_id"], input_hash="failed-run")
    assert mgr.transition(run2, RunStatus.CREATED, RunStatus.RUNNING)
    repo.write_claim(
        ClaimEnvelope(
            fact_id=fact,
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][2],
            observed_ordinal=2,
            world_valid_kind="chapter_proxy",
            world_valid_from=2,
            created_by_run_id=run2,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_yaolao",
            to_entity_id="ent_xiaoyan",
            relation_type="师徒",
            relation_raw="药老正式收萧炎为徒",
        ),
    )
    mgr.fail(run2, "验证失败")
    qs = QueryService(migrated_db, data["book_id"])
    history = qs.claim_history(fact)
    assert len(history) == 1, (
        f"failed run 的版本不得计入（实际 {len(history)}）："
        f"{[h['observed_ordinal'] for h in history]}"
    )
    assert history[0]["observed_ordinal"] == 1


def test_all_events_current_version_only(tmp_path: Path, migrated_db: Engine) -> None:
    """P1（复审）：all_events 按 fact 取当前版本——supersede 的旧事件不返回。"""
    data = seed_active_book(migrated_db, tmp_path)
    # 对林风拜师事件写第 2 版（update）→ 旧版被 supersede
    from novelcanon.schemas.ids import event_fact_id
    from novelcanon.schemas.payloads import EventPayload

    fact = event_fact_id(
        "拜师",
        ["ent_linfeng", "ent_qingyunzong"],
        "ent_qingyunzong",
        data["chapters"][0],
        1,
    )
    repo = Repository(migrated_db)
    new_v = repo.write_claim(
        ClaimEnvelope(
            fact_id=fact,
            claim_version_id="",
            claim_type="event",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][0],
            observed_ordinal=0,
            world_valid_kind="chapter_proxy",
            world_valid_from=0,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        EventPayload(
            event_type="拜师",
            summary="林风正式拜入青云宗成为内门弟子",
            location_entity_id="ent_qingyunzong",
            sequence_in_chapter=1,
        ),
    ).claim_version_id
    qs = QueryService(migrated_db, data["book_id"])
    events = qs.all_events()
    summaries = [e["summary"] for e in events]
    assert "林风拜入青云宗" not in summaries, "被 supersede 的旧事件不得返回"
    assert any("内门弟子" in s for s in summaries), "应返回当前版本"
    assert all(e["claim_version_id"] != data["claims"]["event_linfeng"] for e in events)
    assert new_v in {e["claim_version_id"] for e in events}


def test_event_contested_version_does_not_fall_back(tmp_path: Path, migrated_db: Engine) -> None:
    """P0（三轮）：最新版本 contested → 旧 supported 版本不得回退为当前。"""
    data = seed_active_book(migrated_db, tmp_path)
    from novelcanon.schemas.ids import event_fact_id
    from novelcanon.schemas.payloads import EventPayload

    fact = event_fact_id(
        "拜师",
        ["ent_linfeng", "ent_qingyunzong"],
        "ent_qingyunzong",
        data["chapters"][0],
        1,
    )
    repo = Repository(migrated_db)
    # 第 2 版：contested（证据存疑）
    repo.write_claim(
        ClaimEnvelope(
            fact_id=fact,
            claim_version_id="",
            claim_type="event",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.CONTESTED,
            observed_chapter_id=data["chapters"][0],
            observed_ordinal=0,
            world_valid_kind="chapter_proxy",
            world_valid_from=0,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        EventPayload(
            event_type="拜师",
            summary="林风拜入青云宗（存疑）",
            location_entity_id="ent_qingyunzong",
            sequence_in_chapter=1,
        ),
    )
    qs = QueryService(migrated_db, data["book_id"])
    events = qs.all_events()
    summaries = {e["summary"] for e in events}
    assert "林风拜入青云宗" not in summaries, "旧 supported 版本不得回退为当前版本"
    assert not any("存疑" in s for s in summaries), "contested 版本不得进入默认查询"
    assert "林风拜入青云宗" not in {e["summary"] for e in qs.entity_events("ent_linfeng")}
