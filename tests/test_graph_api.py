"""阶段二 03：实体图谱 API 测试（docs/02-质量验收与产品化阶段/03）。

覆盖：
- /entities 目录（active run + 计数 + 搜索）与错误码；
- /entities/{id} 详情（别名/属性/关系/事件，端点投影为可读名字）；
- /graph 图谱数据（节点 + 当前有效关系边 + cutoff 过滤）；
- 所有端点遵守 book_id 校验与 active run 隔离（复用的过滤逻辑已在
  query.service 的既有测试覆盖，此处只验证端点接线与投影）。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine

from novelcanon.api import create_app
from tests.helpers import seed_active_book
from tests.test_api import make_client


def _client(engine: Engine):
    return make_client(create_app(engine))


def test_entities_lists_active_canonicals(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get("/entities", params={"book_id": data["book_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 7
    items = {i["canonical_id"]: i for i in body["items"]}
    assert "ent_xiaoyan" in items
    x = items["ent_xiaoyan"]
    assert x["canonical_name"] == "萧炎"
    assert "alias_count" in x and "mention_count" in x and "tier" in x


def test_entities_requires_book(tmp_path: Path, migrated_db: Engine) -> None:
    seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get("/entities")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "missing_book"
    r2 = client.get("/entities", params={"book_id": "book_nope"})
    assert r2.status_code == 404
    assert r2.json()["detail"]["code"] == "book_not_found"


def test_entities_search(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get("/entities", params={"book_id": data["book_id"], "q": "萧"})
    assert r.status_code == 200
    names = [i["canonical_name"] for i in r.json()["items"]]
    assert "萧炎" in names


def test_entity_detail(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["canonical_name"] == "萧炎"
    assert body["aliases"] == ["萧炎"]
    assert len(body["states"]) == 1  # seed: 1 条状态
    assert len(body["relations"]) == 2  # 师徒 + 恋人
    for rel in body["relations"]:
        assert rel["from_name"] and rel["to_name"]  # 端点投影为可读名字
    assert len(body["events"]) == 1


def test_entity_detail_not_found(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get(
        "/entities/ent_ghost",
        params={"book_id": data["book_id"]},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "entity_not_found"


def test_graph_nodes_and_edges(tmp_path: Path, migrated_db: Engine) -> None:
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get("/graph", params={"book_id": data["book_id"]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 7
    assert len(body["edges"]) == 2  # 师徒(ch1) + 恋人(ch1)
    node_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids  # 无悬空引用
        assert e["relation_type"] and e["observed_ordinal"] == 1


def test_graph_cutoff_filters_edges(tmp_path: Path, migrated_db: Engine) -> None:
    """cutoff=0：seed 关系都披露于 ch1（ordinal 1）→ 边被过滤。"""
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r = client.get("/graph", params={"book_id": data["book_id"], "knowledge_cutoff": 0})
    assert r.status_code == 200
    assert r.json()["edges"] == []


# ── 复审 P1 回归：cutoff 过滤实体集合 / 跨书隔离 / world_at 生效 ──


def _import_second_book(migrated_db: Engine, tmp_path: Path) -> None:
    """再导入一本书（跨书隔离测试需要存在的另一本书）。"""
    from novelcanon.ingestion.service import import_book
    from tests.helpers import FIXTURE_CHAPTERS, make_fixture_epub

    epub = tmp_path / "second.epub"
    make_fixture_epub(epub, FIXTURE_CHAPTERS, title="第二本")
    import_book(migrated_db, epub, book_id="book_b2")


def test_entities_cutoff_accepted_and_filters(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：cutoff 限制实体集合本身。

    seed 的 alias 全部在 ordinal 0 披露 → cutoff=0 与 cutoff=100 结果
    一致（语义正确：cutoff ≥ 披露章时全返回）；真实库场景（cutoff=0
    → 0 实体，防剧透）已在真实库手工验证——本测试保证 cutoff 参数
    被正确接受且不破坏查询。
    """
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    r0 = client.get("/entities", params={"book_id": data["book_id"], "knowledge_cutoff": 0})
    r1 = client.get("/entities", params={"book_id": data["book_id"], "knowledge_cutoff": 100})
    assert r0.status_code == 200 and r1.status_code == 200
    assert r0.json()["total"] == r1.json()["total"] == 7
    assert len(r0.json()["items"]) == 7


def test_entity_detail_cross_book_isolation(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：一本书的实体 ID 配另一本**存在**的书 book_id → 404。"""
    data = seed_active_book(migrated_db, tmp_path)
    _import_second_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    ok = client.get("/entities/ent_xiaoyan", params={"book_id": data["book_id"]})
    assert ok.status_code == 200
    r = client.get("/entities/ent_xiaoyan", params={"book_id": "book_b2"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "entity_not_found"


def test_entity_detail_world_at_filters_states(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：world_at 必须作用于实体 state（世界时间过滤）。"""
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    full = client.get("/entities/ent_xiaoyan", params={"book_id": data["book_id"]})
    assert full.status_code == 200
    # seed 状态 world_valid_from=披露章；world_at=0 早于所有披露 → 状态全过滤
    wa = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"], "world_at": 0},
    )
    assert wa.status_code == 200
    assert wa.json()["states"] == []


# ── 复审（第三轮）P1 回归：详情 cutoff 不再 500 / 展示名防剧透 ──


def _add_future_alias(engine: Engine, data: dict, cid: str, surface: str, ordinal: int = 2) -> None:
    """给实体补一条指定 ordinal（默认第 3 章=未来章）才披露的表面名
    （防剧透测试用）；write_alias 直接以 canonical 为 canonical_id。"""
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim
    from novelcanon.storage.repository import Repository

    Repository(engine).write_alias(
        AliasClaim(
            alias_fact_id=alias_fact_id(cid, surface),
            claim_version_id="",
            canonical_id=cid,
            surface_name=surface,
            observed_ordinal=ordinal,
            observed_chapter_id=data["chapters"][ordinal],
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        )
    )


def test_entity_detail_with_cutoff_ok(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：详情端点带非零/零 cutoff 不再 500（ao_ord.ordinal → s.ordinal）。

    回归：queries.entity_detail 的别名过滤子查询引用不存在的别名，任何
    带 knowledge_cutoff 的详情请求都会触发 SQLite OperationalError。
    """
    data = seed_active_book(migrated_db, tmp_path)
    client = _client(migrated_db)
    # seed 别名在 ordinal 0 披露 → cutoff=0 仍可见「萧炎」
    r = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"], "knowledge_cutoff": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["aliases"] == ["萧炎"]
    assert "display_name" in body
    # cutoff=100 与无 cutoff 同样正常（不 500）
    for c in (100, None):
        params = {"book_id": data["book_id"]}
        if c is not None:
            params["knowledge_cutoff"] = c
        rr = client.get("/entities/ent_xiaoyan", params=params)
        assert rr.status_code == 200


def test_display_name_cutoff_bounded(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：展示名只由截止章前 active alias 推导——未来章节披露的
    新表面名不得在 cutoff 前泄漏到目录/图谱/详情。"""
    data = seed_active_book(migrated_db, tmp_path)
    _add_future_alias(migrated_db, data, "ent_xiaoyan", "炎帝")
    client = _client(migrated_db)

    # 目录：cutoff=1（炎帝尚未披露）→ 萧炎；cutoff=100 → 最新别名炎帝；
    # 无 cutoff → canonical 名萧炎。canonical_name 字段在 cutoff 时被
    # 替换为安全展示名（对外不返回全书级名称）。
    r1 = client.get("/entities", params={"book_id": data["book_id"], "knowledge_cutoff": 1})
    assert r1.status_code == 200
    x1 = {i["canonical_id"]: i for i in r1.json()["items"]}["ent_xiaoyan"]
    assert x1["canonical_name"] == x1["display_name"] == "萧炎"
    r100 = client.get("/entities", params={"book_id": data["book_id"], "knowledge_cutoff": 100})
    x100 = {i["canonical_id"]: i for i in r100.json()["items"]}["ent_xiaoyan"]
    assert x100["canonical_name"] == x100["display_name"] == "炎帝"
    rfull = client.get("/entities", params={"book_id": data["book_id"]})
    xfull = {i["canonical_id"]: i for i in rfull.json()["items"]}["ent_xiaoyan"]
    assert xfull["canonical_name"] == "萧炎"
    assert xfull["display_name"] == "萧炎"

    # 详情：cutoff=1 → 萧炎（不泄漏炎帝；主体 canonical_name 也被替换）
    d = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"], "knowledge_cutoff": 1},
    )
    assert d.status_code == 200
    body = d.json()
    assert body["canonical_name"] == body["display_name"] == "萧炎"
    assert "炎帝" not in body["aliases"]

    # 图谱节点：cutoff=1 → 节点名萧炎
    g = client.get("/graph", params={"book_id": data["book_id"], "knowledge_cutoff": 1})
    assert g.status_code == 200
    nodes = {n["id"]: n for n in g.json()["nodes"]}
    assert nodes["ent_xiaoyan"]["name"] == "萧炎"


# ── 复审（第四轮）P1 回归：cutoff 响应完整 JSON 无未来名称 / 404 ──


def test_cutoff_response_contains_no_future_names(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：cutoff 响应**完整 JSON 序列化后**不含未来名称——覆盖
    目录（canonical_name 字段替换）、图谱节点、详情主体、关系端点
    from_name/to_name（此前从全书 canonical 名字表生成而泄漏）。"""
    data = seed_active_book(migrated_db, tmp_path)
    _add_future_alias(migrated_db, data, "ent_xiaoyan", "炎帝")
    _add_future_alias(migrated_db, data, "ent_yaolao", "药尊者")
    client = _client(migrated_db)
    future_names = ("炎帝", "药尊者")

    # 目录
    cat = client.get("/entities", params={"book_id": data["book_id"], "knowledge_cutoff": 1})
    assert cat.status_code == 200
    cat_json = json.dumps(cat.json(), ensure_ascii=False)
    for name in future_names:
        assert name not in cat_json, f"目录响应泄漏未来名称 {name}"
    items = {i["canonical_id"]: i for i in cat.json()["items"]}
    assert items["ent_xiaoyan"]["canonical_name"] == items["ent_xiaoyan"]["display_name"] == "萧炎"

    # 图谱
    g = client.get("/graph", params={"book_id": data["book_id"], "knowledge_cutoff": 1})
    assert g.status_code == 200
    g_json = json.dumps(g.json(), ensure_ascii=False)
    for name in future_names:
        assert name not in g_json, f"图谱响应泄漏未来名称 {name}"
    nodes = {n["id"]: n for n in g.json()["nodes"]}
    assert nodes["ent_yaolao"]["name"] == "药老"

    # 详情主体 + 关系端点（mentor: 药老→萧炎；lovers: 萧炎→纳兰嫣然）
    d = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"], "knowledge_cutoff": 1},
    )
    assert d.status_code == 200
    body = d.json()
    d_json = json.dumps(body, ensure_ascii=False)
    for name in future_names:
        assert name not in d_json, f"详情响应泄漏未来名称 {name}"
    assert body["canonical_name"] == body["display_name"] == "萧炎"
    rels = {r["relation_type"]: r for r in body["relations"]}
    assert rels["师徒"]["from_name"] == "药老"
    assert rels["师徒"]["to_name"] == "萧炎"
    assert rels["恋人"]["to_name"] == "纳兰嫣然"


def test_entity_detail_cutoff_entity_not_in_set_404(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：详情实体只披露于 cutoff 之后（不在 cutoff 实体集合）→ 404，
    无 cutoff 时全书可见 → 200。"""
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.types import EntityTier
    from novelcanon.storage.repository import Repository

    data = seed_active_book(migrated_db, tmp_path)
    Repository(migrated_db).upsert_entity(
        EntityRecord(
            canonical_id="ent_future",
            canonical_name="未来角色",
            tier=EntityTier.MAJOR,
            created_by_run_id=data["run_id"],
        )
    )
    _add_future_alias(migrated_db, data, "ent_future", "未来角色")
    client = _client(migrated_db)
    r = client.get(
        "/entities/ent_future",
        params={"book_id": data["book_id"], "knowledge_cutoff": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "entity_not_found"
    ok = client.get("/entities/ent_future", params={"book_id": data["book_id"]})
    assert ok.status_code == 200


# ── 复审（第五轮）P1/P2 回归：cutoff 搜索防探测 / 安全名缺失回退 ──


def test_entities_search_cutoff_no_future_names(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P1：cutoff 搜索禁止匹配全书 canonical_name，alias 搜索也限
    截止章前——未来名称探测不得命中早期实体（名称↔实体映射不泄漏）。

    - 搜索截止后名称 → 0 结果（未来 alias、未来 canonical 名）；
    - 搜索截止前安全名 → 正常命中。
    """
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.types import EntityTier
    from novelcanon.storage.repository import Repository

    data = seed_active_book(migrated_db, tmp_path)
    # 未来 alias：炎帝(xiaoyan, ord2)、药尊者(yaolao, ord2)
    _add_future_alias(migrated_db, data, "ent_xiaoyan", "炎帝")
    _add_future_alias(migrated_db, data, "ent_yaolao", "药尊者")
    # canonical 名只在「未来」披露的实体：canonical_name=未来真名，
    # 截止章前唯一 alias 是旧名（ordinal 0）
    Repository(migrated_db).upsert_entity(
        EntityRecord(
            canonical_id="ent_canon",
            canonical_name="未来真名",
            tier=EntityTier.MAJOR,
            created_by_run_id=data["run_id"],
        )
    )
    _add_future_alias(migrated_db, data, "ent_canon", "旧名", ordinal=0)
    client = _client(migrated_db)
    b = data["book_id"]

    def _search(q: str, cutoff: int | None) -> list[str]:
        params = {"book_id": b, "q": q}
        if cutoff is not None:
            params["knowledge_cutoff"] = cutoff
        r = client.get("/entities", params=params)
        assert r.status_code == 200
        return [i["canonical_id"] for i in r.json()["items"]]

    # 未来 alias 名：cutoff=1 不命中；无 cutoff 命中
    assert _search("炎帝", 1) == []
    assert _search("药尊者", 1) == []
    assert "ent_xiaoyan" in _search("炎帝", None)
    # 未来 canonical 名：cutoff=1 不命中（禁止 canonical_name 匹配）；
    # 无 cutoff 经 canonical_name 命中
    assert _search("未来真名", 1) == []
    assert _search("未来真名", None) == ["ent_canon"]
    # 截止前安全名：正常命中
    assert "ent_xiaoyan" in _search("萧炎", 1)
    assert "ent_yaolao" in _search("药老", 1)
    assert "ent_canon" in _search("旧名", 1)
    # 模糊前缀匹配同样被 cutoff 约束
    assert _search("萧", 1) != []


def test_relation_endpoint_safe_fallback_no_alias(tmp_path: Path, migrated_db: Engine) -> None:
    """复审 P2：关系端点实体没有任何 alias 时，cutoff 下回退**实体 ID**
    而非全书 canonical 名（严格防剧透，不泄漏名称）。"""
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import relation_fact_id
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.payloads import RelationPayload
    from novelcanon.schemas.types import ClaimStatus, EntityTier, Operation
    from novelcanon.storage.repository import Repository

    data = seed_active_book(migrated_db, tmp_path)
    repo = Repository(migrated_db)
    repo.upsert_entity(
        EntityRecord(
            canonical_id="ent_noalias",
            canonical_name="无别名未来角色",
            tier=EntityTier.MAJOR,
            created_by_run_id=data["run_id"],
        )
    )
    repo.write_claim(
        ClaimEnvelope(
            fact_id=relation_fact_id("ent_noalias", "相识", "ent_xiaoyan"),
            claim_version_id="",
            claim_type="relation",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=data["chapters"][0],
            observed_ordinal=0,
            created_by_run_id=data["run_id"],
            created_at="2026-01-01T00:00:00+00:00",
        ),
        RelationPayload(
            from_entity_id="ent_noalias",
            to_entity_id="ent_xiaoyan",
            relation_type="相识",
            relation_raw="测试关系",
        ),
    )
    client = _client(migrated_db)
    d = client.get(
        "/entities/ent_xiaoyan",
        params={"book_id": data["book_id"], "knowledge_cutoff": 1},
    )
    assert d.status_code == 200
    body = d.json()
    rels = {r["relation_type"]: r for r in body["relations"]}
    assert rels["相识"]["from_name"] == "ent_noalias"  # 回退实体 ID
    assert "无别名未来角色" not in json.dumps(body, ensure_ascii=False)
