"""实体图谱只读查询（阶段二 03：实体图谱 API 与前端）。

隔离契约（复审 P1 逐条闭环）：
- **实体集合 = 投影后的 canonical**：消歧目标（entity_resolutions 的
  canonical_id，active run）+ 未消歧的孤立 mention（alias 观察 active
  且不在任何 resolution 的 mention 侧）。消歧前 mention 实体不单独
  展示——「丽贝卡 ×N」合并为 canonical「丽贝卡」，mention_count 反映
  合并规模；
- **cutoff 过滤实体集合**：canonical 在 cutoff 前须有 alias 观察
  （mention 的出现章 ordinal）才可见——cutoff=0 不得暴露全书实体
  （防剧透）；
- **book 隔离**：实体必须在该书 active run 的数据中出现（resolutions
  run_id / alias 观察 run），否则 404——一本书的实体 ID 配另一本书的
  book_id 拿不到数据；
- **world_at 作用于 state**：详情带 world_at 时状态用
  QueryService.world_state_at（世界时间有效区间），不带时用当前版本；
- **display_name（复审 P1）**：目录/图谱/详情带 cutoff 时展示名只由
   「截止章前 active-run alias」推导（与 QueryService.display_name 同源
   语义）——e.canonical_name 可能来自未来章节（防剧透）；
- 属性/关系/事件复用 query.service.QueryService 的同源过滤逻辑，不在
  本模块重写业务规则。
"""

from __future__ import annotations

from sqlalchemy import Engine, text


def _active_run_ids_sql(book_id: str, params: dict[str, object]) -> str:
    """active run 子查询（SQL 片段；params 需已含 book_id 键 :b）。"""
    del book_id, params  # 仅生成 SQL 片段，参数统一走外层 :b
    return "SELECT run_id FROM extraction_runs WHERE status = 'active' AND book_id = :b"


def _entity_set_sql(params: dict[str, object] | None = None, *, cutoff: bool = False) -> str:
    """投影后 canonical 实体集合 SQL 片段（active run + 该书，可选 cutoff）。

    目录（entity_catalog）与 inspect active 统计共用同一口径——「产品
    目录」即权威计数（复审 P1：inspect entities 必须等于目录去重数，
    不得混入 mention 型 canonical）。params 需含 :b；cutoff=True 时还需
    :cutoff。未消歧 mention 的判定也限定该书 active run——历史消歧结果
    不得隐藏本 run 的孤立 mention（复审 P1）。
    """
    cutoff_sql = ""
    if cutoff:
        cutoff_sql = "AND a.observed_ordinal <= :cutoff"
    active = _active_run_ids_sql("", params or {})
    return (
        # 消歧 canonical：cutoff 前有 mention 的 alias 观察
        "SELECT r.canonical_id FROM entity_resolutions r"
        f" WHERE r.run_id IN ({active})"
        "  AND EXISTS (SELECT 1 FROM entity_alias_claims a"
        "    JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
        "    JOIN extraction_runs r2 ON r2.run_id = ao.extraction_run_id"
        "    WHERE a.canonical_id = r.mention_id AND a.operation = 'assert'"
        "      AND r2.status = 'active' AND r2.book_id = :b"
        f"      {cutoff_sql})"
        " UNION"
        # 未消歧 mention：alias 观察 active + cutoff 内
        " SELECT a.canonical_id FROM entity_alias_claims a"
        "  JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
        "  JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
        "  WHERE r.status = 'active' AND r.book_id = :b AND a.operation = 'assert'"
        "    AND a.canonical_id NOT IN (SELECT mention_id FROM entity_resolutions"
        f"      WHERE run_id IN ({active}))"
        f"  {cutoff_sql}"
    )


def _book_exists(engine: Engine, book_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT book_id FROM books WHERE book_id = :b"), {"b": book_id}
        ).fetchone()
    return row is not None


def entity_catalog(
    engine: Engine,
    book_id: str,
    *,
    cutoff: int | None = None,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """实体目录：投影后的 canonical 集合（active run，cutoff 过滤）。

    - 展示集合：resolutions canonical（cutoff 前有 alias 观察）∪
      未消歧 mention（alias 观察 active 且 cutoff 内）；
    - mention_count：canonical 名下 resolution mention 数（合并规模）；
    - alias_count：canonical 名下 DISTINCT 表面名（经 resolution 投影
      + 未消歧 mention 自身）；
    - 排序：importance → mention_count → alias_count（消歧合并的
      主要人物靠前，孤立 mention 靠后）。
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    params: dict[str, object] = {"b": book_id}
    cutoff_sql = ""
    if cutoff is not None:
        params["cutoff"] = cutoff
        cutoff_sql = "AND a.observed_ordinal <= :cutoff"

    entity_set = _entity_set_sql(params, cutoff=cutoff is not None)

    # 展示名（复审 P1）：带 cutoff 时只由截止章前 active-run alias 推导
    # （与 QueryService.display_name 同源语义：最新披露的表面名）——
    # e.canonical_name 可能来自未来章节，防剧透；无 cutoff 时全书可见，
    # 直接展示 canonical_name。
    if cutoff is not None:
        display_name_sql = (
            "COALESCE((SELECT a.surface_name FROM entity_alias_claims a"
            "  JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
            "  JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
            "  LEFT JOIN entity_resolutions er ON er.mention_id = a.canonical_id"
            "    AND er.run_id IN (SELECT run_id FROM extraction_runs"
            "      WHERE status = 'active' AND book_id = :b)"
            "  WHERE COALESCE(er.canonical_id, a.canonical_id) = e.canonical_id"
            "    AND r.status = 'active' AND r.book_id = :b"
            f"    {cutoff_sql}"
            "  ORDER BY a.observed_ordinal DESC, a.rowid DESC LIMIT 1"
            "), e.canonical_name)"
        )
    else:
        display_name_sql = "e.canonical_name"

    alias_count_sql = (
        " (SELECT COUNT(DISTINCT s.surface_name) FROM ("
        "   SELECT a.surface_name FROM entity_alias_claims a"
        "    JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
        "    JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
        "    JOIN entity_resolutions r2 ON r2.mention_id = a.canonical_id"
        "      AND r2.run_id IN (SELECT run_id FROM extraction_runs"
        "        WHERE status = 'active' AND book_id = :b)"
        "    WHERE a.operation = 'assert' AND r.status = 'active' AND r.book_id = :b"
        "      AND r2.canonical_id = e.canonical_id"
        f"      {cutoff_sql}"
        "   UNION"
        "   SELECT a.surface_name FROM entity_alias_claims a"
        "    JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
        "    JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
        "    WHERE a.operation = 'assert' AND r.status = 'active' AND r.book_id = :b"
        "      AND a.canonical_id = e.canonical_id"
        f"      {cutoff_sql}"
        " ) s)"
    )
    mention_count_sql = (
        " (SELECT COUNT(*) FROM entity_resolutions m"
        f"   WHERE m.canonical_id = e.canonical_id"
        f"     AND m.run_id IN ({_active_run_ids_sql(book_id, params)}))"
    )

    conds = [f"e.canonical_id IN ({entity_set})"]
    if q.strip():
        params["q"] = f"%{q.strip()}%"
        if cutoff is not None:
            # 复审 P1：cutoff 时搜索**禁止匹配全书 canonical_name**（未来
            # 名称探测会泄漏名称↔实体映射），只匹配截止章前 active-run
            # alias（observed_ordinal <= cutoff）。
            conds.append(
                "EXISTS (SELECT 1 FROM entity_alias_claims a3"
                "  JOIN alias_observations ao3 ON ao3.claim_version_id = a3.claim_version_id"
                "  JOIN extraction_runs r3 ON r3.run_id = ao3.extraction_run_id"
                "  WHERE a3.operation = 'assert' AND r3.status = 'active'"
                "    AND r3.book_id = :b AND a3.surface_name LIKE :q"
                f"    AND a3.observed_ordinal <= :cutoff"
                "    AND (a3.canonical_id = e.canonical_id"
                "         OR a3.canonical_id IN (SELECT r4.mention_id FROM entity_resolutions r4"
                "           WHERE r4.canonical_id = e.canonical_id"
                f"             AND r4.run_id IN ({_active_run_ids_sql(book_id, params)}))))"
            )
        else:
            conds.append(
                "(e.canonical_name LIKE :q OR EXISTS (SELECT 1 FROM entity_alias_claims a3"
                "  JOIN alias_observations ao3 ON ao3.claim_version_id = a3.claim_version_id"
                "  JOIN extraction_runs r3 ON r3.run_id = ao3.extraction_run_id"
                "  WHERE a3.operation = 'assert' AND r3.status = 'active' AND r3.book_id = :b"
                "    AND a3.surface_name LIKE :q"
                "    AND (a3.canonical_id = e.canonical_id"
                "         OR a3.canonical_id IN (SELECT r4.mention_id FROM entity_resolutions r4"
                "           WHERE r4.canonical_id = e.canonical_id"
                f"             AND r4.run_id IN ({_active_run_ids_sql(book_id, params)})))))"
            )
    where = " AND ".join(conds)

    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT COUNT(*) FROM entities e WHERE {where}"),
            params,
        ).scalar_one()
        # 复审 P1：cutoff 存在时对外**不返回全书级 canonical_name**——
        # canonical_name 字段本身替换为安全展示名（客户端读 JSON 也拿不到
        # 未来名称）；无 cutoff 时全书可见，原样返回。
        name_col = display_name_sql if cutoff is not None else "e.canonical_name"
        rows = (
            conn.execute(
                text(
                    "SELECT e.canonical_id,"
                    f" {name_col} AS canonical_name,"
                    " e.tier,"
                    " COALESCE(e.importance_score, 0.0) AS importance_score,"
                    f"{display_name_sql} AS display_name,"
                    f"{alias_count_sql} AS alias_count,"
                    f"{mention_count_sql} AS mention_count"
                    " FROM entities e"
                    f" WHERE {where}"
                    " ORDER BY importance_score DESC, mention_count DESC, alias_count DESC"
                    " LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": limit, "offset": offset},
            )
            .mappings()
            .fetchall()
        )
    return {
        "book_id": book_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


def _entity_in_book(engine: Engine, book_id: str, canonical_id: str) -> bool:
    """实体是否属于该书 active run（多书隔离，复审 P1）。

    命中条件：canonical 是 resolutions 目标（active run 该书）或
    alias 观察（active run 该书）——一本书的实体 ID 配另一本书的
    book_id 返回 False。
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM entity_resolutions r"
                " WHERE r.canonical_id = :c"
                "   AND r.run_id IN (SELECT run_id FROM extraction_runs"
                "     WHERE status = 'active' AND book_id = :b)"
                " UNION"
                " SELECT 1 FROM entity_alias_claims a"
                " JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
                " JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
                " WHERE a.canonical_id = :c AND a.operation = 'assert'"
                "   AND r.status = 'active' AND r.book_id = :b"
                " LIMIT 1"
            ),
            {"c": canonical_id, "b": book_id},
        ).fetchone()
    return row is not None


def entity_detail(
    engine: Engine,
    book_id: str,
    canonical_id: str,
    *,
    cutoff: int | None = None,
    world_at: int | None = None,
) -> dict | None:
    """实体详情：表面名 + 当前属性 + 一跳关系 + 参与事件（含证据）。

    - book 隔离：实体不在该书 active run → None（404）；
    - world_at 传入时属性用世界时间状态（world_state_at），否则当前版本；
    - 关系/事件端点投影为可读名字。
    """
    from novelcanon.query.service import QueryService

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT canonical_id, canonical_name, tier, importance_score,"
                    " created_by_run_id, created_at"
                    " FROM entities WHERE canonical_id = :c"
                ),
                {"c": canonical_id},
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            return None
        base = dict(row)
    if not _entity_in_book(engine, book_id, canonical_id):
        return None
    # 复审 P1：cutoff 时实体必须属于 cutoff 前的可见实体集合——只披露于
    # 未来章节的实体（如 alias 观察晚于 cutoff）详情直接 404（防剧透）。
    if cutoff is not None:
        with engine.connect() as conn:
            in_set = conn.execute(
                text(
                    f"SELECT 1 FROM ({_entity_set_sql(cutoff=True)}) s"
                    " WHERE s.canonical_id = :c LIMIT 1"
                ),
                {"b": book_id, "c": canonical_id, "cutoff": cutoff},
            ).fetchone()
        if in_set is None:
            return None

    params: dict[str, object] = {"b": book_id, "c": canonical_id}
    cutoff_sql = ""
    if cutoff is not None:
        params["cutoff"] = cutoff
        # 子查询结束后的可见别名是 s（列 ordinal = MIN(observed_ordinal)）
        cutoff_sql = "AND s.ordinal <= :cutoff"
    with engine.connect() as conn:
        aliases = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT s.surface_name FROM ("
                    "  SELECT a.surface_name, MIN(a.observed_ordinal) AS ordinal"
                    "   FROM entity_alias_claims a"
                    "   JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
                    "   JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
                    "   JOIN entity_resolutions r2 ON r2.mention_id = a.canonical_id"
                    "     AND r2.run_id IN (SELECT run_id FROM extraction_runs"
                    "       WHERE status = 'active' AND book_id = :b)"
                    "   WHERE a.operation = 'assert' AND r.status = 'active'"
                    "     AND r.book_id = :b AND r2.canonical_id = :c"
                    "   GROUP BY a.surface_name"
                    "  UNION"
                    "  SELECT a.surface_name, MIN(a.observed_ordinal) AS ordinal"
                    "   FROM entity_alias_claims a"
                    "   JOIN alias_observations ao ON ao.claim_version_id = a.claim_version_id"
                    "   JOIN extraction_runs r ON r.run_id = ao.extraction_run_id"
                    "   WHERE a.operation = 'assert' AND r.status = 'active'"
                    "     AND r.book_id = :b AND a.canonical_id = :c"
                    "   GROUP BY a.surface_name"
                    ") s"
                    f" WHERE 1=1 {cutoff_sql}"
                    " ORDER BY s.surface_name"
                ),
                params,
            ).fetchall()
        ]

    svc = QueryService(engine, book_id)
    # mention → canonical → 名字投影（详情页关系/事件端点可读展示；
    # 未消歧的 mention 原样显示自身表面名）
    with engine.connect() as conn:
        resolved = {
            m: c
            for m, c in conn.execute(
                text(
                    "SELECT m.mention_id, m.canonical_id FROM entity_resolutions m"
                    " WHERE m.run_id IN (SELECT run_id FROM extraction_runs"
                    "   WHERE status = 'active' AND book_id = :b)"
                ),
                {"b": book_id},
            ).fetchall()
        }
        names = {
            r[0]: r[1]
            for r in conn.execute(
                text("SELECT canonical_id, canonical_name FROM entities")
            ).fetchall()
        }

    def _display(mention_id: str) -> tuple[str, str]:
        cid = resolved.get(mention_id, mention_id)
        if cutoff is not None:
            # 复审 P1：关系端点名称也走 cutoff-safe 展示名解析器——同一
            # active-run、cutoff-aware 语义，不能从全书 canonical 名字表取
            # （否则主体安全、端点却泄漏未来名称）。
            return cid, _safe_display(cid)
        return cid, names.get(cid, mention_id)

    _safe_cache: dict[str, str] = {}

    def _safe_display(cid: str) -> str:
        if cid not in _safe_cache:
            # 复审 P2：严格防剧透——查不到安全 alias 时回退**实体 ID**
            # （可审计、不含名称），不回退全书 canonical 名（names 表）。
            _safe_cache[cid] = svc.display_name(cid, knowledge_cutoff=cutoff) or cid
        return _safe_cache[cid]

    # world_at 生效：属性用世界时间状态（仅该时间点有效），否则当前版本
    if world_at is not None:
        states = svc.world_state_at(canonical_id, world_at, knowledge_cutoff=cutoff)
    else:
        states = svc.entity_state(canonical_id, knowledge_cutoff=cutoff)
    relations = svc.one_hop_relations(canonical_id, knowledge_cutoff=cutoff, world_at=world_at)
    for r in relations:
        r["from_name"] = _display(r["from_entity_id"])[1]
        r["to_name"] = _display(r["to_entity_id"])[1]
    # 证据回填原文 span（03 退出条件：节点/边可展开来源章节与原文证据）
    _attach_evidence_text(engine, book_id, relations)
    # 展示名（复审 P1）：带 cutoff 时只由截止前 active alias 推导（防
    # 剧透），与目录/图谱同源；无 cutoff 时全书可见，用 canonical 名。
    display_name = _safe_display(canonical_id) if cutoff is not None else base["canonical_name"]
    # 复审 P1：cutoff 时对外不返回真实 canonical_name——字段替换为安全
    # 展示名（客户端读 JSON 也拿不到未来名称）。
    if cutoff is not None:
        base["canonical_name"] = display_name
    return {
        **base,
        "display_name": display_name,
        "aliases": aliases,
        "states": states,
        "relations": relations,
        "events": svc.entity_events(canonical_id, knowledge_cutoff=cutoff, world_at=world_at),
    }


def _attach_evidence_text(engine: Engine, book_id: str, items: list[dict]) -> None:
    """给 items[*].evidence 回填 span_text（章内偏移 → 书全文切片）。

    与 api._attach_span_text 同源逻辑；定位缺失的证据跳过。
    """
    ev_rows: list[dict] = []
    for it in items:
        for ev in it.get("evidence", []):
            if (
                ev.get("chapter_id")
                and ev.get("char_start") is not None
                and ev.get("char_end") is not None
            ):
                ev_rows.append(ev)
    if not ev_rows:
        return
    chapter_ids = {ev["chapter_id"] for ev in ev_rows}
    ph = ", ".join(f":c{n}" for n in range(len(chapter_ids)))
    params: dict[str, object] = {f"c{n}": c for n, c in enumerate(chapter_ids)}
    with engine.connect() as conn:
        bounds = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                text(
                    f"SELECT chapter_id, char_start, char_end, ordinal FROM chapters"
                    f" WHERE chapter_id IN ({ph})"
                ),
                params,
            ).fetchall()
        }
    from novelcanon.storage.repository import Repository

    full = Repository(engine).get_book_text(book_id)
    for ev in ev_rows:
        b = bounds.get(ev.get("chapter_id"))
        cs, ce = ev.get("char_start"), ev.get("char_end")
        if b is not None and isinstance(cs, int) and isinstance(ce, int) and cs < ce:
            start = b[0] + max(0, cs)
            end = b[0] + min(ce, b[1] - b[0])
            ev["span_text"] = full[start:end] if start < end else ""
            ev["observed_ordinal"] = b[2]


def graph_data(
    engine: Engine,
    book_id: str,
    *,
    cutoff: int | None = None,
    world_at: int | None = None,
    limit: int = 80,
    min_importance: float = 0.0,
) -> dict:
    """图谱数据：节点 = 投影后 canonical 实体（cutoff 过滤），边 = 当前
    有效关系（每 fact 最新版本 + supported + 非 retract，双时间过滤）。

    边只保留两端都在返回节点集合内的——前端渲染时不会出现悬空引用。
    """
    catalog = entity_catalog(engine, book_id, cutoff=cutoff, limit=limit)
    nodes = [
        {
            "id": i["canonical_id"],
            "name": i["display_name"],
            "tier": i["tier"],
            "importance": i["importance_score"],
            "alias_count": i["alias_count"],
            "mention_count": i["mention_count"],
        }
        for i in catalog["items"]
        if i["importance_score"] >= min_importance
    ]
    node_ids = {n["id"] for n in nodes}
    if not node_ids:
        return {"book_id": book_id, "nodes": [], "edges": [], "total_nodes": catalog["total"]}

    cutoff_sql = ""
    world_sql = ""
    params: dict[str, object] = {"book": book_id}
    if cutoff is not None:
        cutoff_sql = "AND c.observed_ordinal <= :cutoff"
        params["cutoff"] = cutoff
    if world_at is not None:
        world_sql = (
            " AND ("
            "   (c.world_valid_kind = 'story_time'"
            "    AND c.world_valid_from <= :world"
            "    AND (c.world_valid_to IS NULL OR c.world_valid_to >= :world))"
            "   OR (c.world_valid_kind = 'chapter_proxy'"
            "    AND c.world_valid_from <= :world)"
            " )"
        )
        params["world"] = world_at
    # 关系 claims 的实体引用是 mention_id（章级 namespace）；经
    # entity_resolutions 投影到 canonical（active run）——与查询层
    # entity_scope 同源，未消歧的 mention 原样透传。
    mention_projection = (
        "COALESCE((SELECT r2.canonical_id FROM entity_resolutions r2"
        "   WHERE r2.mention_id = :m AND r2.run_id IN (SELECT run_id"
        "     FROM extraction_runs WHERE status = 'active' AND book_id = :book)"
        "   LIMIT 1), :m)"
    )
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT q.claim_version_id, q.fact_id, q.observed_ordinal,"
                    " q.confidence, q.direction,"
                    f" {mention_projection.replace(':m', 'q.from_entity_id')} AS from_canonical,"
                    f" {mention_projection.replace(':m', 'q.to_entity_id')} AS to_canonical,"
                    " q.relation_type, q.relation_raw"
                    " FROM ("
                    "  SELECT c.claim_version_id, c.fact_id, c.observed_ordinal,"
                    "         c.operation, c.claim_status, c.confidence, c.world_valid_kind,"
                    "         r.from_entity_id, r.to_entity_id, r.relation_type,"
                    "         r.relation_raw, r.direction,"
                    "         ROW_NUMBER() OVER (PARTITION BY c.fact_id"
                    "           ORDER BY c._rowid DESC) rn"
                    "  FROM v_active_claims c"
                    "  JOIN relation_claims r ON r.claim_version_id = c.claim_version_id"
                    "  WHERE c.book_id = :book"
                    f"  {cutoff_sql}{world_sql}"
                    ") q"
                    " WHERE q.rn = 1 AND q.operation != 'retract' AND q.claim_status = 'supported'"
                    " ORDER BY q.observed_ordinal, q.fact_id"
                ),
                params,
            )
            .mappings()
            .fetchall()
        )
    edges = []
    for r in rows:
        frm, to = r["from_canonical"], r["to_canonical"]
        if frm not in node_ids or to not in node_ids:
            continue
        edges.append(
            {
                "source": frm,
                "target": to,
                "relation_type": r["relation_type"],
                "relation_raw": r["relation_raw"],
                "direction": r["direction"] or "undirected",
                "observed_ordinal": r["observed_ordinal"],
                "confidence": r["confidence"],
            }
        )
    return {
        "book_id": book_id,
        "nodes": nodes,
        "edges": edges,
        "total_nodes": catalog["total"],
    }
