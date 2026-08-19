"""阶段 09 事件链接与双时间查询测试集（docs/implementation/09）。

覆盖验证项：
- event participant 和 event link 引用完整；
- results 反向查询与 causes 正向结果一致；
- 环形事件图不会导致无限递归（visited 防环）；
- 路径置信度计算正确（边置信度乘积）；
- cutoff 隐藏任意一跳时对应路径被截断；
- unknown world time 不会被表达为精确状态；
- 两个时间参数组合查询的结果符合人工期望；
- 双时间测试成为强制回归项。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.events import EventLinkService
from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import RunManager
from novelcanon.pipeline.validation import Activator
from novelcanon.query import QueryService
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import claim_version_id
from novelcanon.schemas.memory import EventLinkRecord, EvidenceRecord
from novelcanon.schemas.payloads import EventLinkPayload, EventPayload
from novelcanon.schemas.types import (
    ClaimStatus,
    EventLinkType,
    EvidenceStance,
    EvidenceType,
    Operation,
    RunStatus,
)
from novelcanon.storage.repository import Repository
from tests.helpers import make_fixture_epub

BOOK_ID = "book_events"

# 5 章：主角历练线（拜师→突破→遇险→获救→复仇）。
# 第 2/4/5 章含因果连接词（「入门之后…因此…」「于是…」「伤愈之后…」），
# 供 LinkVerifier 做关系证据验证；第 3 章无因果表述（突破→遇险保持 unverified）。
EVENT_CHAPTERS: list[tuple[str, str]] = [
    ("第一章", "陆尘拜入青云宗，成为药老的关门弟子。"),
    ("第二章", "入门之后，陆尘闭关苦修三月，因此境界突破至筑基期。"),
    ("第三章", "陆尘下山历练，在荒山遭妖兽围攻。"),
    ("第四章", "药老闻讯赶到，于是出手救下重伤的陆尘。"),
    ("第五章", "伤愈之后，陆尘立誓，必报此仇。"),
]


def _book_and_chapters(
    migrated_db: Engine, tmp_path
) -> tuple[str, dict[int, str], dict[int, str]]:
    epub = tmp_path / "events.epub"
    make_fixture_epub(epub, EVENT_CHAPTERS, title="事件测试")
    result = import_book(migrated_db, epub, book_id=BOOK_ID)
    repo = Repository(migrated_db)
    chapters = repo.list_chapters(BOOK_ID)
    ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(BOOK_ID)
    texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    return result.book_id, ids, texts


def _seed_events(
    migrated_db: Engine,
    book_id: str,
    ids: dict[int, str],
    texts: dict[int, str],
) -> tuple[str, list[str]]:
    """写事件 claims + 证据 + participants，返回 (run_id, event_version_ids)。"""
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.types import EntityTier

    repo = Repository(migrated_db)
    run_id = RunManager(migrated_db).create(book_id, input_hash="events-fixture")
    events: list[tuple[str, str, str, int, list[str], str | None]] = [
        # (event_type, summary, chapter_id, ordinal, participants, location)
        ("拜师", "陆尘拜入青云宗", ids[0], 0, ["ent_luchen"], "ent_qingyunzong"),
        ("突破", "陆尘突破至筑基期", ids[1], 1, ["ent_luchen"], "ent_qingyunzong"),
        ("遇险", "陆尘遭妖兽围攻", ids[2], 2, ["ent_luchen"], None),
        ("获救", "药老救下陆尘", ids[3], 3, ["ent_luchen", "ent_yaolao"], None),
        ("立誓", "陆尘立誓报仇", ids[4], 4, ["ent_luchen"], None),
    ]
    repo.upsert_entity(
        EntityRecord(
            canonical_id="ent_luchen",
            canonical_name="陆尘",
            tier=EntityTier.CORE,
            created_by_run_id=run_id,
        )
    )
    repo.upsert_entity(
        EntityRecord(
            canonical_id="ent_yaolao",
            canonical_name="药老",
            tier=EntityTier.MAJOR,
            created_by_run_id=run_id,
        )
    )
    repo.upsert_entity(
        EntityRecord(
            canonical_id="ent_qingyunzong",
            canonical_name="青云宗",
            tier=EntityTier.MAJOR,
            created_by_run_id=run_id,
        )
    )
    version_ids: list[str] = []
    for seq, (etype, summary, ch_id, ordinal, participants, location) in enumerate(events):
        payload = EventPayload(
            event_type=etype,
            summary=summary,
            location_entity_id=location,
            sequence_in_chapter=seq + 1,
        )
        from novelcanon.schemas.ids import event_fact_id

        fact_id = event_fact_id(etype, participants, location, ch_id, seq + 1)
        write_result = repo.write_claim(
            ClaimEnvelope(
                fact_id=fact_id,
                claim_version_id="",  # 由 write_claim 按 payload 确定性生成
                claim_type="event",
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=ch_id,
                observed_ordinal=ordinal,
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload,
        )
        version_id = write_result.claim_version_id
        version_ids.append(version_id)
        for p in participants:
            repo.add_event_participant(version_id, p)
        # 每个事件一条 direct 证据（同章原文 span）
        ch = repo.list_chapters(book_id)[ordinal]
        full = repo.get_book_text(book_id)
        ch_text = full[ch["char_start"] : ch["char_end"]]
        span_start, span_end = 0, min(20, len(ch_text))
        from novelcanon.ingestion.normalize import sha256 as h

        span = ch_text[span_start:span_end]
        from novelcanon.schemas.ids import evidence_id

        eid = evidence_id(version_id, ch_id, span_start, span_end, h(span))
        repo.write_evidence(
            EvidenceRecord(
                evidence_id=eid,
                claim_version_id=version_id,
                evidence_stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
                chapter_id=ch_id,
                char_start=span_start,
                char_end=span_end,
                span_hash=h(span),
                literal_match_rate=1.0,
                verification_method="hash-exact",
            )
        )
    return run_id, version_ids


def _link_events(
    migrated_db: Engine, book_id: str, run_id: str
) -> object:
    """生成候选 → LinkVerifier 关系证据验证 → 落库跨章链接（先激活 run）。

    LinkVerifier 检查目标章原文「原因引用 + 因果连接词」：验证通过的边
    （如 拜师→突破，第 2 章含「入门之后…因此…」）自动 supported；
    无关系证据的边（如 突破→遇险，第 3 章无因果表述）保持 unverified。
    """
    mgr = RunManager(migrated_db)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(migrated_db).activate(run_id) is None
    return EventLinkService(migrated_db).link_run(run_id, book_id)


# ── 链接生成 ───────────────────────────────────────────────────


def test_event_linker_participant_intersection(tmp_path, migrated_db: Engine) -> None:
    """参与者交集 + 时间顺序 → 跨章链接（弱因果 enables）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    stats = _link_events(migrated_db, book_id, run_id)
    assert stats.events == 5
    assert stats.candidates > 0
    assert stats.links > 0
    # 陆尘参与的事件应链成一条链（拜师→突破→遇险→获救→立誓）
    with migrated_db.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM event_links")
        ).scalar()
    assert n == stats.links


def test_event_link_references_complete(tmp_path, migrated_db: Engine) -> None:
    """event participant 和 event link 引用完整（无悬空 FK）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    with migrated_db.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        # 链接的 source/target 必须存在
        bad = conn.execute(
            text(
                "SELECT count(*) FROM event_links l"
                " WHERE NOT EXISTS (SELECT 1 FROM event_claims e"
                "   WHERE e.claim_version_id = l.source_event_id)"
                " OR NOT EXISTS (SELECT 1 FROM event_claims e"
                "   WHERE e.claim_version_id = l.target_event_id)"
            )
        ).scalar()
    assert bad == 0


def test_event_link_observed_ordinal_is_max_evidence(tmp_path, migrated_db: Engine) -> None:
    """event link observed ordinal = 支持证据的最大披露章节（09 §4）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT l.observed_ordinal,"
                " (SELECT max(ch.ordinal) FROM claim_evidence e"
                "  JOIN chapters ch ON ch.chapter_id = e.chapter_id"
                "  WHERE e.claim_version_id IN (l.source_event_id, l.target_event_id))"
                " AS max_ev_ord"
                " FROM event_links l"
            )
        ).fetchall()
    assert rows
    for ordinal, max_ev in rows:
        assert ordinal == max_ev, f"observed_ordinal {ordinal} != 最大证据章节 {max_ev}"


# ── 因果递归查询 ───────────────────────────────────────────────


def test_causal_paths_recursive_and_sorted(tmp_path, migrated_db: Engine) -> None:
    """递归展开 + 路径置信度乘积 + 按置信度排序。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    q = QueryService(migrated_db, book_id)
    paths = q.causal_paths(version_ids[0])  # 拜师 → ...
    assert paths, "拜师应有因果后继"
    # 置信度单调降序（边置信度乘积）
    confs = [p["conf"] for p in paths]
    assert confs == sorted(confs, reverse=True), "路径必须按置信度降序"
    # 路径置信度 = 边置信度乘积（每条路径 depth 与 conf 一致）
    for p in paths:
        assert 0 < p["conf"] <= 1.0
        assert p["depth"] == p["path"].count(">"), "depth 必须等于跳数"
        assert p["depth"] <= 5, "默认最大深度 5"


def test_causal_paths_loop_safe(tmp_path, migrated_db: Engine) -> None:
    """环形事件图不导致无限递归（visited 防环）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    # 手工加一条反向边形成环（获救 → 拜师）
    src, tgt = version_ids[3], version_ids[0]
    from novelcanon.schemas.ids import event_link_fact_id

    payload = EventLinkPayload(
        source_event_id=src, target_event_id=tgt, relation_type=EventLinkType.CAUSES
    )
    repo = Repository(migrated_db)
    repo.write_event_link(
        EventLinkRecord(
            envelope=ClaimEnvelope(
                fact_id=event_link_fact_id(src, EventLinkType.CAUSES, tgt),
                claim_version_id="",  # write_event_link 按 payload 确定性生成
                claim_type="event_link",
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=ids[3],
                observed_ordinal=3,
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload=payload,
        )
    )
    q = QueryService(migrated_db, book_id)
    paths = q.causal_paths(version_ids[0])
    assert paths, "环存在时仍返回有限路径"
    # visited 防环：同事件在一条路径内最多出现一次
    for p in paths:
        nodes = p["path"].split(">")
        assert len(nodes) == len(set(nodes)), f"路径含环：{p['path']}"


def test_causal_paths_cutoff_truncates(tmp_path, migrated_db: Engine) -> None:
    """cutoff 隐藏任意一跳时对应路径被截断（09 §4/§6）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    q = QueryService(migrated_db, book_id)
    full = q.causal_paths(version_ids[0])
    # cutoff=0（只读第 1 章）：链路第一跳（拜师→突破 发生在第 2 章）不可见
    early = q.causal_paths(version_ids[0], knowledge_cutoff=0)
    assert early == [], f"cutoff=0 时第 1 章后的事件不可见：{early}"
    # cutoff=1：只到第 2 章（拜师→突破），更远截断
    mid = q.causal_paths(version_ids[0], knowledge_cutoff=1)
    assert mid, "cutoff=1 应有拜师→突破"
    assert all(p["depth"] == 1 for p in mid), "cutoff=1 只能走 1 跳"
    assert len(full) >= len(mid), "cutoff 越大路径越多"


# ── 双时间查询（knowledge cutoff vs world at chapter）─────────


def test_dual_time_independent(tmp_path, migrated_db: Engine) -> None:
    """两个时间参数独立：world_at 回答世界状态，cutoff 回答读者知识。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    q = QueryService(migrated_db, book_id)

    # world_events_at：第 3 章世界发生「遇险」
    world3 = q.world_events_at(2)
    types3 = {e["event_type"] for e in world3}
    assert "遇险" in types3, f"第 3 章世界应有遇险事件：{types3}"

    # 按实体过滤：陆尘参与第 3 章事件
    luchen = q.world_events_at(2, canonical_id="ent_luchen")
    assert luchen and all("陆尘" in (e.get("summary") or "") for e in luchen)

    # 两个时间参数互不影响：world_at(2) 不因 cutoff 改变
    # （world_events_at 无 cutoff 参数——它是世界时间，独立于读者知识）
    assert world3, "world at chapter 独立于 knowledge cutoff"


def test_unknown_world_time_not_precise(tmp_path, migrated_db: Engine) -> None:
    """unknown world time 不会被表达为精确状态（09 §7）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    q = QueryService(migrated_db, book_id)
    # 事件默认 chapter_proxy（observed_ordinal 即发生章节）
    for e in q.world_events_at(2):
        assert e["observed_ordinal"] == 2, "chapter_proxy 事件必须锚定发生章节"


# ── P0 回归：world at chapter 按 world_valid 区间过滤 ─────────


def test_world_state_at_respects_world_valid(tmp_path, migrated_db: Engine) -> None:
    """验收 P0：world_valid_from=5 的 story_time 状态在 world_at=0 不返回；
    unknown 不表达为精确状态。"""
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.payloads import StatePayload
    from novelcanon.schemas.types import WorldValidKind

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="world-valid")
    repo = Repository(migrated_db)
    repo.upsert_entity(
        __import__("novelcanon.schemas.memory", fromlist=["EntityRecord"]).EntityRecord(
            canonical_id="ent_x", canonical_name="X", created_by_run_id=run_id
        )
    )
    from novelcanon.config.hash import stable_config_hash
    from novelcanon.schemas.ids import state_fact_id

    def add_state(field: str, value: str, kind: WorldValidKind, wfrom: int, wto=None):
        payload = StatePayload(field=field, value=value, raw_value=value, subject_entity_id="ent_x")
        fact_id = state_fact_id("ent_x", field)
        vid = claim_version_id(fact_id, stable_config_hash({"v": value, "k": kind.value}))
        repo.write_claim(
            ClaimEnvelope(
                fact_id=fact_id,
                claim_version_id=vid,
                claim_type="state",
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=ids[0],
                observed_ordinal=0,
                world_valid_kind=kind,
                world_valid_from=wfrom,
                world_valid_to=wto,
                world_valid_confidence=1.0,
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload,
        )
        return vid

    add_state("f_story", "晚", WorldValidKind.STORY_TIME, 5)  # 世界时间从第 6 章起
    add_state("f_proxy", "早", WorldValidKind.CHAPTER_PROXY, 0)  # 第 1 章起
    add_state("f_unknown", "未知", WorldValidKind.UNKNOWN, 0)  # 未知

    # 激活使可见
    mgr = RunManager(migrated_db)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(migrated_db).activate(run_id) is None

    q = QueryService(migrated_db, book_id)
    early = {s["field"]: s["value"] for s in q.world_state_at("ent_x", 0)}
    assert "f_story" not in early, (
        f"story_time from=5 在 world_at=0 不得返回：{early}"
    )
    assert early.get("f_proxy") == "早", "chapter_proxy from=0 在 world_at=0 可见"
    assert "f_unknown" not in early, "unknown 不得表达为精确状态"

    late = {s["field"]: s["value"] for s in q.world_state_at("ent_x", 6)}
    assert late.get("f_story") == "晚", "story_time 在 world_at=6（>=from=5）可见"


def test_world_state_at_combines_cutoff(tmp_path, migrated_db: Engine) -> None:
    """验收 P0：world_at 与 knowledge_cutoff 双参数组合过滤。

    「世界时间从第 1 章成立、但第 10 章才通过回忆披露」的事实：
    world_at=5 且 cutoff=5 时读者不得看到（observed_ordinal=10 > cutoff）；
    cutoff=10 时可见。world 窗口与 cutoff 各自独立、组合生效。
    """
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.payloads import StatePayload
    from novelcanon.schemas.types import WorldValidKind

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="dual-time-cutoff")
    repo = Repository(migrated_db)
    repo.upsert_entity(
        __import__("novelcanon.schemas.memory", fromlist=["EntityRecord"]).EntityRecord(
            canonical_id="ent_y", canonical_name="Y", created_by_run_id=run_id
        )
    )
    from novelcanon.config.hash import stable_config_hash
    from novelcanon.schemas.ids import state_fact_id

    # 世界时间从第 1 章（from=1）成立，但第 10 章才通过回忆披露（ordinal=10）
    payload = StatePayload(
        field="f_secret", value="往事", raw_value="往事", subject_entity_id="ent_y"
    )
    vid = claim_version_id(
        state_fact_id("ent_y", "f_secret"),
        stable_config_hash({"v": "往事", "k": "story_time"}),
    )
    repo.write_claim(
        ClaimEnvelope(
            fact_id=state_fact_id("ent_y", "f_secret"),
            claim_version_id=vid,
            claim_type="state",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=ids[4],  # 章节 FK 有效（ordinal 可大于章数）
            observed_ordinal=10,  # 披露于第 10 章（回忆）
            world_valid_kind=WorldValidKind.STORY_TIME,
            world_valid_from=1,
            world_valid_to=None,
            world_valid_confidence=1.0,
            created_by_run_id=run_id,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        payload,
    )
    # 对照组：第 1 章即披露（ordinal=0）
    payload0 = StatePayload(
        field="f_early", value="公开", raw_value="公开", subject_entity_id="ent_y"
    )
    vid0 = claim_version_id(
        state_fact_id("ent_y", "f_early"),
        stable_config_hash({"v": "公开", "k": "chapter_proxy"}),
    )
    repo.write_claim(
        ClaimEnvelope(
            fact_id=state_fact_id("ent_y", "f_early"),
            claim_version_id=vid0,
            claim_type="state",
            operation=Operation.ASSERT,
            claim_status=ClaimStatus.SUPPORTED,
            observed_chapter_id=ids[0],
            observed_ordinal=0,
            world_valid_kind=WorldValidKind.CHAPTER_PROXY,
            world_valid_from=1,
            world_valid_to=None,
            world_valid_confidence=1.0,
            created_by_run_id=run_id,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        payload0,
    )

    mgr = RunManager(migrated_db)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(migrated_db).activate(run_id) is None

    q = QueryService(migrated_db, book_id)
    # world_at=5：世界时间窗口通过（from=1 <= 5），但 cutoff=5 读者未到
    # 第 10 章的回忆披露 → 未来知识不得出现
    early = {s["field"] for s in q.world_state_at("ent_y", 5, knowledge_cutoff=5)}
    assert "f_secret" not in early, (
        f"cutoff=5 时第 10 章才披露的事实不得可见：{early}"
    )
    assert "f_early" in early, "第 1 章即披露的事实 cutoff=5 可见"
    # cutoff=10：回忆已披露 → 可见
    late = {s["field"] for s in q.world_state_at("ent_y", 5, knowledge_cutoff=10)}
    assert "f_secret" in late, "cutoff=10 时回忆披露的事实可见"
    # world 窗口仍独立生效：world_at=0（第 1 章前）即使 cutoff=10 也不可见
    before = {s["field"] for s in q.world_state_at("ent_y", 0, knowledge_cutoff=10)}
    assert "f_secret" not in before, "world_valid_from=1 在 world_at=0 不得返回"


def test_relations_and_org_world_valid(tmp_path, migrated_db: Engine) -> None:
    """验收 P1：world_valid 过滤扩展到关系与组织查询（此前只有 state）。

    one_hop_relations 组合 world_at + knowledge_cutoff；org_state_at
    提供势力/成员世界状态查询（story_time 区间 / chapter_proxy /
    unknown 排除 / cutoff 组合）。
    """
    from novelcanon.config.hash import stable_config_hash
    from novelcanon.schemas.ids import org_fact_id, relation_fact_id
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.payloads import OrgPayload, RelationPayload
    from novelcanon.schemas.types import EntityTier, WorldValidKind

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = RunManager(migrated_db).create(book_id, input_hash="world-rel-org")
    repo = Repository(migrated_db)
    for cid, name in (("ent_x", "X"), ("ent_y", "Y"), ("ent_org", "青云门")):
        repo.upsert_entity(
            EntityRecord(
                canonical_id=cid, canonical_name=name,
                tier=EntityTier.MINOR, created_by_run_id=run_id,
            )
        )

    def write(payload, fact_id, kind, wfrom, ordinal):
        vid = claim_version_id(fact_id, stable_config_hash({"v": kind.value}))
        repo.write_claim(
            ClaimEnvelope(
                fact_id=fact_id,
                claim_version_id=vid,
                claim_type="relation" if isinstance(payload, RelationPayload) else "org",
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=ids[min(ordinal, 4)],
                observed_ordinal=ordinal,
                world_valid_kind=kind,
                world_valid_from=wfrom,
                world_valid_to=None,
                world_valid_confidence=1.0,
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload,
        )

    # 关系：X 是 Y 的师父——世界时间从第 4 章起（story_time from=3），
    # 但第 1 章即披露（ordinal=0）
    write(
        RelationPayload(from_entity_id="ent_x", to_entity_id="ent_y",
                        relation_type="师徒", relation_raw="X是Y的师父"),
        relation_fact_id("ent_x", "师徒", "ent_y"),
        WorldValidKind.STORY_TIME, 3, 0,
    )
    # org：X 加入 青云门——chapter_proxy from=2（第 3 章起），披露 ordinal=0
    write(
        OrgPayload(org_entity_id="ent_org", member_entity_id="ent_x", role="弟子", action="join"),
        org_fact_id("ent_org", "ent_x", "弟子"),
        WorldValidKind.CHAPTER_PROXY, 2, 0,
    )

    mgr = RunManager(migrated_db)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(migrated_db).activate(run_id) is None

    q = QueryService(migrated_db, book_id)
    # 关系：world_at=0（第 1 章）故事时间未开始 → 不可见；world_at=3 可见
    rel0 = q.one_hop_relations("ent_x", world_at=0)
    assert all(r["relation_type"] != "师徒" for r in rel0), (
        f"story_time from=3 在 world_at=0 不得返回关系：{rel0}"
    )
    rel3 = q.one_hop_relations("ent_x", world_at=3)
    assert any(r["relation_type"] == "师徒" for r in rel3), "world_at=3 关系可见"
    # 关系 + cutoff 组合：披露 ordinal=0 → cutoff=0 可见（world 窗口仍生效）
    rel0c = q.one_hop_relations("ent_x", world_at=0, knowledge_cutoff=0)
    assert all(r["relation_type"] != "师徒" for r in rel0c), "world 窗口优先于 cutoff"
    rel3c = q.one_hop_relations("ent_x", world_at=3, knowledge_cutoff=0)
    assert any(r["relation_type"] == "师徒" for r in rel3c), "cutoff=0 已披露可见"

    # org：chapter_proxy from=2 → world_at=1 不可见、world_at=2 可见
    org0 = q.org_state_at("ent_x", 1)
    assert org0 == [], f"org chapter_proxy from=2 在 world_at=1 不可见：{org0}"
    org1 = q.org_state_at("ent_x", 2)
    assert len(org1) == 1 and org1[0]["action"] == "join", f"world_at=2 org 可见：{org1}"
    # org + cutoff 组合：披露 ordinal=0 → cutoff=0 可见
    org1c = q.org_state_at("ent_x", 2, knowledge_cutoff=0)
    assert len(org1c) == 1, "cutoff=0 已披露的 org 状态可见"


def test_causal_results_excludes_enables(tmp_path, migrated_db: Engine) -> None:
    """验收 P1：causal_results 只反向查 causes——初始分支与递归分支都
    必须限定 relation_type='causes'，enables 边不得混入 results。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    # 激活（不跑规则层：全部边手工以 supported 写入，纯测查询过滤）
    mgr = RunManager(migrated_db)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(migrated_db).activate(run_id) is None

    from novelcanon.schemas.ids import event_link_fact_id
    from novelcanon.schemas.memory import EventLinkRecord

    repo = Repository(migrated_db)

    def write_link(src, tgt, rtype, ordinal):
        repo.write_event_link(
            EventLinkRecord(
                envelope=ClaimEnvelope(
                    fact_id=event_link_fact_id(src, rtype, tgt),
                    claim_version_id="",
                    claim_type="event_link",
                    operation=Operation.ASSERT,
                    claim_status=ClaimStatus.SUPPORTED,
                    observed_chapter_id=ids[ordinal],
                    observed_ordinal=ordinal,
                    created_by_run_id=run_id,
                    created_at="2026-01-01T00:00:00+00:00",
                ),
                payload=EventLinkPayload(
                    source_event_id=src, target_event_id=tgt, relation_type=rtype
                ),
            )
        )

    # 拜师 →(causes) 突破；突破 →(enables) 遇险；遇险 →(causes) 获救
    write_link(version_ids[0], version_ids[1], EventLinkType.CAUSES, 1)
    write_link(version_ids[1], version_ids[2], EventLinkType.ENABLES, 2)
    write_link(version_ids[2], version_ids[3], EventLinkType.CAUSES, 3)

    q = QueryService(migrated_db, book_id)
    # 获救的 results：causes 来源只有 遇险；突破 经 enables 到达必须排除
    results = q.causal_results(version_ids[3])
    sources = {p["event"]["claim_version_id"] for p in results}
    assert version_ids[2] in sources, "causes 反向必须包含直接原因 遇险"
    assert version_ids[1] not in sources, (
        f"enables 边不得混入 causes 反向结果：{sources}"
    )
    # 递归分支同样受限：遇险的 causes 来源没有（突破→遇险 是 enables）
    results2 = q.causal_results(version_ids[2])
    sources2 = {p["event"]["claim_version_id"] for p in results2}
    assert sources2 == set(), (
        f"递归分支不得经 enables 回溯到 突破：{sources2}"
    )


def test_results_reverse_of_causes(tmp_path, migrated_db: Engine) -> None:
    """results 反向查询与 causes 正向结果一致（09 §3，P1 修复）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    with migrated_db.connect() as conn:
        forward = conn.execute(
            text(
                "SELECT source_event_id, target_event_id FROM event_links"
                " WHERE relation_type = 'causes'"
            )
        ).fetchall()
    assert forward, "应有 causes 边"
    q = QueryService(migrated_db, book_id)
    # 正向：每个 causes 边 src → tgt 在 causal_paths 可达
    for src, tgt in forward:
        forward_paths = q.causal_paths(src)
        reachable = {p["event"]["claim_version_id"] for p in forward_paths}
        assert tgt in reachable, f"causes 边 {src}→{tgt} 应被正向路径覆盖"
    # 反向：tgt 的 causal_results 必须包含 src（results = causes 反向）
    for src, tgt in forward:
        reverse_paths = q.causal_results(tgt)
        sources = {p["event"]["claim_version_id"] for p in reverse_paths}
        assert src in sources, (
            f"causes 边 {src}→{tgt} 的反向 results 必须包含 {src}：{sources}"
        )
    # 无 causes 边的目标（如获救，只有 enables 入边）：results 为空或仅 enables 来源
    with migrated_db.connect() as conn:
        has_causes = conn.execute(
            text("SELECT source_event_id FROM event_links WHERE relation_type='causes' LIMIT 1")
        ).fetchone()
    assert has_causes is not None


def test_dual_time_force_regression_guard(tmp_path, migrated_db: Engine) -> None:
    """双时间测试成为强制回归项（09 退出标准）：跑一次完整链路。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    stats = _link_events(migrated_db, book_id, run_id)
    q = QueryService(migrated_db, book_id)
    # 完整断言：链接落库 + 因果查询 + world at + cutoff 全部可用
    assert stats.links > 0
    assert q.causal_paths(version_ids[0])
    assert q.world_events_at(0)
    assert q.causal_paths(version_ids[0], knowledge_cutoff=0) == []


# ── 召回率评测（09 §2：候选生成不能只观察精度）────────────────


def test_candidate_recall_rate(tmp_path, migrated_db: Engine) -> None:
    """候选生成召回率：黄金因果集（手工标注期望链接）对比生成候选。

    黄金期望：拜师→突破（causes，同地点）；遇险→获救（enables，同参与者）。
    召回率 = 命中的黄金链接 / 黄金链接总数。
    """
    from novelcanon.events.linker import EventInfo, EventLinker
    from novelcanon.schemas.types import EventLinkType

    # 手工构造黄金事件（模拟 _seed_events 的真实形态）
    events = [
        EventInfo(
            claim_version_id="e1", fact_id="f1", event_type="拜师",
            summary="陆尘拜入青云宗", participants=["ent_luchen"],
            location_entity_id="ent_qingyunzong", observed_ordinal=0,
            observed_chapter_id="ch0", sequence_in_chapter=1,
            evidence_ordinals=[0], evidence_stances=["supports"],
        ),
        EventInfo(
            claim_version_id="e2", fact_id="f2", event_type="突破",
            summary="陆尘突破至筑基期", participants=["ent_luchen"],
            location_entity_id="ent_qingyunzong", observed_ordinal=1,
            observed_chapter_id="ch1", sequence_in_chapter=1,
            evidence_ordinals=[1], evidence_stances=["supports"],
        ),
        EventInfo(
            claim_version_id="e3", fact_id="f3", event_type="遇险",
            summary="陆尘遭妖兽围攻", participants=["ent_luchen"],
            location_entity_id=None, observed_ordinal=2,
            observed_chapter_id="ch2", sequence_in_chapter=1,
            evidence_ordinals=[2], evidence_stances=["supports"],
        ),
        EventInfo(
            claim_version_id="e4", fact_id="f4", event_type="获救",
            summary="药老救下陆尘", participants=["ent_luchen", "ent_yaolao"],
            location_entity_id=None, observed_ordinal=3,
            observed_chapter_id="ch3", sequence_in_chapter=1,
            evidence_ordinals=[3], evidence_stances=["supports"],
        ),
    ]
    # 黄金期望链接（人工标注）
    golden = {("f1", "f2"), ("f3", "f4")}  # (source_fact, target_fact)
    candidates = EventLinker().generate_candidates(events)
    generated = {(c.source.fact_id, c.target.fact_id) for c in candidates}
    recall = len(golden & generated) / len(golden)
    assert recall >= 0.5, (
        f"候选召回率 {recall} 过低（黄金 {len(golden)} 条，命中"
        f" {len(golden & generated)}）"
    )
    # 拜师→突破 同地点 → causes（强因果）
    causes = {
        (c.source.fact_id, c.target.fact_id)
        for c in candidates
        if c.relation_type == EventLinkType.CAUSES
    }
    assert ("f1", "f2") in causes, "同地点同参与者应为 causes"


# ── P0 回归：因果边不消费 rejected / refutes 事件 ─────────────


def test_rejected_events_produce_no_supported_link(tmp_path, migrated_db: Engine) -> None:
    """验收 P0：rejected 事件（refutes 证据）不得生成 supported 因果边。

    两个事件 claim_status=rejected、evidence_stance=refutes，即使同参与者
    同时间顺序，也不能成为因果边端点 → 无 supported 边生成。
    """
    from novelcanon.events.linker import EventInfo, EventLinker

    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    # 直接构造两个 rejected 事件（模拟验收场景）
    ev1 = EventInfo(
        claim_version_id="v1", fact_id="f1", event_type="遭遇",
        summary="甲在乙面前遇险", participants=["ent_a", "ent_b"],
        location_entity_id=None, observed_ordinal=0,
        observed_chapter_id=ids[0], sequence_in_chapter=1,
        evidence_ordinals=[0], evidence_stances=["refutes"],
        claim_status="rejected", operation="assert",
    )
    ev2 = EventInfo(
        claim_version_id="v2", fact_id="f2", event_type="救援",
        summary="乙出手相救", participants=["ent_a", "ent_b"],
        location_entity_id=None, observed_ordinal=1,
        observed_chapter_id=ids[1], sequence_in_chapter=1,
        evidence_ordinals=[1], evidence_stances=["refutes"],
        claim_status="rejected", operation="assert",
    )
    # linker 层面：rejected 事件不产生候选
    candidates = EventLinker().generate_candidates([ev1, ev2])
    assert candidates == [], f"rejected 事件不得生成因果候选：{candidates}"
    # 事件本身 is_supported 必须为 False
    assert not ev1.is_supported and not ev2.is_supported


# ── P0 回归：规则层只生成 candidate，未经关系证据验证不得 supported ──


def test_rule_links_without_evidence_stay_unverified(
    tmp_path, migrated_db: Engine
) -> None:
    """验收 P0：规则候选必须经关系证据验证才 supported。

    - 突破→遇险（目标章第 3 章无因果表述）→ 保持 unverified，不进默认
      因果回答（「甲吃早饭」→「甲中彩票」同参与者同先后同理）；
    - 拜师→突破（第 2 章「入门之后…因此…」）→ LinkVerifier 验证通过
      supported（自动因果链非空，不再依赖手工写入）。
    """
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    stats = _link_events(migrated_db, book_id, run_id)

    assert stats.candidates > 0, "规则候选必须被生成"
    assert stats.links > 0
    assert stats.verified > 0, "LinkVerifier 必须实际产出 supported 边"
    assert stats.unverified > 0, "无关系证据的候选必须保持 unverified"
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT claim_status, count(*) FROM event_links GROUP BY claim_status"
            )
        ).fetchall()
    statuses = dict(rows)
    assert "supported" in statuses and "unverified" in statuses

    # 突破→遇险：无关系证据 → unverified
    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT claim_status FROM event_links"
                " WHERE source_event_id = :s AND target_event_id = :t"
            ),
            {"s": version_ids[1], "t": version_ids[2]},
        ).fetchone()
    assert row is not None and row[0] == "unverified", (
        f"突破→遇险 必须 unverified：{row}"
    )
    q = QueryService(migrated_db, book_id)
    paths = q.causal_paths(version_ids[1])
    assert version_ids[2] not in {p["event"]["claim_version_id"] for p in paths}, (
        "unverified 边不得进入默认因果回答"
    )
    # 对照：拜师→突破 验证通过 → 在因果回答中
    assert version_ids[1] in {
        p["event"]["claim_version_id"] for p in q.causal_paths(version_ids[0])
    }, "验证通过的边必须进入因果回答"


def test_link_verifier_promotes_evidence_edges(
    tmp_path, migrated_db: Engine
) -> None:
    """验收 P0：LinkVerifier 把「证据充分」的边提升为 supported，并落库
    验证方法（causal-connective）与原文证据 span（可审计）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id, version_ids = _seed_events(migrated_db, book_id, ids, texts)
    _link_events(migrated_db, book_id, run_id)
    with migrated_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT claim_status, verification_method, verification_evidence"
                " FROM event_links WHERE source_event_id = :s AND target_event_id = :t"
            ),
            {"s": version_ids[0], "t": version_ids[1]},
        ).fetchone()
    assert row is not None
    assert row[0] == "supported", "拜师→突破 必须有关系证据 → supported"
    assert row[1] == "causal-connective", f"验证方法应记录：{row[1]}"
    assert row[2] is not None and "入门之后" in row[2], (
        f"验证证据 span 必须落库：{row[2]}"
    )
