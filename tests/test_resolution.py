"""阶段 08 实体消歧黄金测试集（docs/implementation/08）。

覆盖验证项：
- canonical ID 在相同输入和配置下稳定（同 surface 跨 run 复用）；
- 同名不同人物不被规则误合并（林风 vs 林锋）；
- 后期身份揭示可以在 canonical 层合并（seed alias：小石 → 林风）；
- 早期 cutoff 查询不会展示后期名称（cutoff-safe 三窗口）；
- unresolved 数量/占比可统计（泛称过滤）；
- merge/split 决策可追溯（entity_merge_audit）；
- 全部 mention 处于 canonical mapped 或 unresolved 之一。
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from novelcanon.ingestion.service import import_book
from novelcanon.pipeline import RunManager
from novelcanon.query import QueryService
from novelcanon.resolution import (
    EntityResolver,
    ResolutionService,
    is_generic,
    normalize_surface,
)
from novelcanon.storage.repository import Repository
from tests.helpers import make_fixture_epub

BOOK_ID = "book_resolve"

# 8 章：早期小石 + 林锋（散修），后期披露林风（正式名），再有小荷/铁匠/青云子
RESOLVE_CHAPTERS: list[tuple[str, str]] = [
    ("第一章", "小镇上，小石正在劈柴，他是铁匠的学徒。小石与铁匠之女小荷定了亲。"),
    ("第二章", "青云宗弟子路过，看中小石根骨，要收他入门。铁匠大喜。"),
    ("第三章", "小石拜入青云子门下，测出灵根，突破至金丹期。散修林锋在旁观礼。"),
    ("第四章", "众人这才知道，小石真名林风，是失踪的林家少主。"),
    ("第五章", "林风与青云子约定五年后下山寻仇。林锋说自己也要去。"),
    ("第六章", "小荷送林风到镇口，两人依依惜别。铁匠嘱咐他照顾好自己。"),
    ("第七章", "林风回到小镇，发现小荷与铁匠已搬走。林锋嘲笑他扑了个空。"),
    ("第八章", "林风在林家祖宅找到小荷留下的信。信上说，她与铁匠去了南疆。"),
]


def _book_and_chapters(migrated_db: Engine, tmp_path) -> tuple[str, dict[int, str], dict[int, str]]:
    epub = tmp_path / "resolve.epub"
    make_fixture_epub(epub, RESOLVE_CHAPTERS, title="消歧测试")
    result = import_book(migrated_db, epub, book_id=BOOK_ID)
    repo = Repository(migrated_db)
    chapters = repo.list_chapters(BOOK_ID)
    ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(BOOK_ID)
    texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}
    return result.book_id, ids, texts


def _seed_mentions(migrated_db: Engine, book_id: str, ids, texts) -> str:
    """构造 run + 手写 mentions（模拟 stage 07 materialize 结果），返回 run_id。"""
    from novelcanon.schemas.memory import EntityRecord
    from novelcanon.schemas.types import EntityTier

    repo = Repository(migrated_db)
    run_id = RunManager(migrated_db).create(book_id, input_hash="resolve-fixture")
    # 按章写入 mentions（mention_id 章级 namespace，同 stage 07）
    mentions: list[tuple[str, str, str, str]] = []  # (mention_id, chapter, surface, canonical)
    for ordinal, (_title, ch_text) in enumerate(RESOLVE_CHAPTERS):
        ch_id = ids[ordinal]
        prefix = ch_id[:12]
        surfaces = []
        if "小石" in ch_text:
            surfaces.append(("小石", "小石"))
        if "林风" in ch_text:
            surfaces.append(("林风", "林风"))
        if "林锋" in ch_text:
            surfaces.append(("林锋", "林锋"))
        if "铁匠" in ch_text:
            surfaces.append(("铁匠", "铁匠"))
        if "小荷" in ch_text:
            surfaces.append(("小荷", "小荷"))
        if "青云子" in ch_text:
            surfaces.append(("青云子", "青云子"))
        for i, (surface, _) in enumerate(surfaces):
            mid = f"{prefix}_m{ordinal}_{i}"
            mentions.append((mid, ch_id, surface, mid))
    for mid, ch_id, surface, canonical in mentions:
        repo.upsert_entity(
            EntityRecord(
                canonical_id=canonical,
                canonical_name=surface,
                tier=EntityTier.MINOR,
                created_by_run_id=run_id,
            )
        )
        repo.write_mention(mid, ch_id, surface, run_id, canonical_id=canonical)
    return run_id


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "resolutions": conn.execute(
                text("SELECT count(*) FROM entity_resolutions")
            ).scalar(),
            "unresolved": conn.execute(
                text("SELECT count(*) FROM unresolved_mentions")
            ).scalar(),
            "merges": conn.execute(
                text("SELECT count(*) FROM entity_merge_audit")
            ).scalar(),
        }


# ── 名称规范化和泛称过滤 ───────────────────────────────────────


def test_normalize_surface() -> None:
    """全半角/空白/标点统一（保留原文写法）。"""
    assert normalize_surface(" 萧 炎 ") == "萧炎"
    assert normalize_surface("萧炎　") == "萧炎"
    assert normalize_surface("Ｃｉａｌ") == "Cial"
    assert normalize_surface("「萧炎」") == "「萧炎」"  # 原样保留可安全归一的标点


def test_generic_filter() -> None:
    """泛称不创建实体（08 §1）。"""
    for generic in ("男子", "中年男子", "少女", "长老", "母亲", "众人"):
        assert is_generic(generic), f"{generic} 应为泛称"
    assert not is_generic("萧炎")
    assert not is_generic("林家")


# ── resolver 确定性规则 ────────────────────────────────────────


def test_resolver_same_surface_same_canonical() -> None:
    """规范化 surface 相同 → 同一 canonical（跨章合并）。"""
    r = EntityResolver()
    plan = r.resolve(
        [
            {"mention_id": "a", "surface_name": "萧炎"},
            {"mention_id": "b", "surface_name": "萧炎"},
            {"mention_id": "c", "surface_name": "萧炎 "},  # 规范化后相同
        ]
    )
    canonicals = {m.canonical_id for m in plan.resolved}
    assert len(canonicals) == 1, "同 surface 必须同 canonical"
    assert len(plan.resolved) == 3
    assert plan.unresolved == []


def test_resolver_distinct_names_distinct_canonical() -> None:
    """同名不同人物（林风 vs 林锋）不被规则误合并。"""
    r = EntityResolver()
    plan = r.resolve(
        [
            {"mention_id": "a", "surface_name": "林风"},
            {"mention_id": "b", "surface_name": "林锋"},
            {"mention_id": "c", "surface_name": "林风"},
        ]
    )
    canonicals = {m.canonical_id for m in plan.resolved}
    assert len(canonicals) == 2, "林风/林锋 必须不同 canonical"


def test_resolver_generic_to_unresolved() -> None:
    """泛称 → unresolved（不创建实体）。"""
    r = EntityResolver()
    plan = r.resolve(
        [
            {"mention_id": "a", "surface_name": "萧炎"},
            {"mention_id": "b", "surface_name": "中年男子"},
        ]
    )
    assert len(plan.resolved) == 1
    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].reason == "generic"


def test_resolver_seed_alias_identity_reveal() -> None:
    """后期身份揭示（小石 → 林风）经 seed alias 在 canonical 层合并。"""
    r = EntityResolver()
    # 库里已有：第四章披露「小石真名林风」→ alias 小石 → ent_linfeng_canonical
    canonical = "ent_linfeng_1"
    r.seed({"小石": canonical, "林风": canonical})
    plan = r.resolve(
        [
            {"mention_id": "a", "surface_name": "小石"},
            {"mention_id": "b", "surface_name": "林风"},
        ]
    )
    canonicals = {m.canonical_id for m in plan.resolved}
    assert canonicals == {canonical}, "seed alias 必须把别名合并到同一 canonical"


def test_resolver_idempotent_stable() -> None:
    """相同输入 → 相同 canonical 分配（P0：干净重建同输入同 ID）。"""
    r1 = EntityResolver()
    r2 = EntityResolver()
    mentions = [
        {"mention_id": "a", "surface_name": "萧炎", "chapter_id": "ch1"},
        {"mention_id": "b", "surface_name": "萧薰儿", "chapter_id": "ch1"},
        {"mention_id": "c", "surface_name": "萧炎", "chapter_id": "ch2"},
    ]
    p1 = r1.resolve(mentions)
    p2 = r2.resolve(mentions)
    m1 = {m.mention_id: m.canonical_id for m in p1.resolved}
    m2 = {m.mention_id: m.canonical_id for m in p2.resolved}
    # 独立 resolver 相同输入必须得到相同 canonical_id（确定性 hash）
    assert m1 == m2, f"确定性 canonical_id 必须一致：{m1} vs {m2}"
    assert m1["a"] == m1["c"], "跨章同 surface 必须同 canonical"


def test_resolver_same_chapter_same_name_not_merged() -> None:
    """P0 回归：同一章内两个同 surface mention（同名不同人物）不合并。"""
    r = EntityResolver()
    plan = r.resolve(
        [
            {"mention_id": "a", "surface_name": "王明", "chapter_id": "ch1"},
            {"mention_id": "b", "surface_name": "王明", "chapter_id": "ch1"},
            {"mention_id": "c", "surface_name": "王明", "chapter_id": "ch2"},
        ]
    )
    canonicals = {m.canonical_id for m in plan.resolved}
    assert len(canonicals) == 3, (
        f"同章同名不同人物不得误合并（a/b 同章应独立）：{canonicals}"
    )
    reasons = {m.mention_id: m.reason for m in plan.resolved}
    assert reasons["a"] == "same-chapter-name-conflict"
    # 跨章 c 与 a 的 surface 相同但 a 已冲突 → 独立（保守：不猜测）
    assert canonicals == {
        m.canonical_id for m in plan.resolved
    }  # 三个独立 ID


# ── service 落库 + 投影 ────────────────────────────────────────


def test_resolution_service_end_to_end(tmp_path, migrated_db: Engine) -> None:
    """整 run 消歧：mention 投影、canonical 实体、merge audit、幂等。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = _seed_mentions(migrated_db, book_id, ids, texts)

    service = ResolutionService(migrated_db)
    stats = service.resolve_run(run_id, book_id)
    assert stats.mentions > 0
    assert stats.mapped > 0
    assert stats.merges > 0, "章级 namespace 实体应被 merge 到 canonical"

    counts = _counts(migrated_db)
    assert counts["resolutions"] == stats.mapped
    assert counts["merges"] == stats.merges

    # 幂等：重跑不增加 resolution/audit（mention_id 主键 + audit 无唯一约束需查重）
    before = _counts(migrated_db)
    service.resolve_run(run_id, book_id)
    after = _counts(migrated_db)
    assert before["resolutions"] == after["resolutions"]
    assert before["merges"] == after["merges"], "重复 resolve 不得重复 merge audit"

    # 全部 mention 处于 mapped 或 unresolved 之一（退出标准）
    with migrated_db.connect() as conn:
        mapped = conn.execute(
            text("SELECT count(*) FROM entity_resolutions WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
        unresolved = conn.execute(
            text("SELECT count(*) FROM unresolved_mentions WHERE run_id = :r"),
            {"r": run_id},
        ).scalar()
    assert mapped + unresolved == stats.mentions, (
        "每个 mention 必须 mapped 或 unresolved"
    )


def test_merge_audit_traceable(tmp_path, migrated_db: Engine) -> None:
    """merge 决策可追溯（from/to/reason/run）。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = _seed_mentions(migrated_db, book_id, ids, texts)
    ResolutionService(migrated_db).resolve_run(run_id, book_id)
    repo = Repository(migrated_db)
    audits = repo.merge_audit()
    assert audits, "必须有 merge 审计"
    for a in audits:
        assert a["action"] == "merge"
        assert a["from_entity_id"]
        assert a["to_entity_id"]
        assert a["run_id"] == run_id
        assert a["reason"] == "exact-surface-match"


# ── cutoff-safe 展示名（阶段 08 §7 + 09 §1）────────────────────


def test_cutoff_safe_display_name(tmp_path, migrated_db: Engine) -> None:
    """早期/中期/全书三窗口：不展示后期名称。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = _seed_mentions(migrated_db, book_id, ids, texts)
    service = ResolutionService(migrated_db)
    service.resolve_run(run_id, book_id)

    # 找到 林风 canonical（经 seed alias 小石→林风 合并）
    canonical = None
    for m in _mentions_of(migrated_db):
        if m["surface_name"] == "小石":
            with migrated_db.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT canonical_id FROM entity_resolutions WHERE mention_id = :m"
                    ),
                    {"m": m["mention_id"]},
                ).fetchone()
            canonical = row[0] if row else None
            break
    assert canonical is not None

    # 写 alias claims：小石（ordinal 0 起）与林风（ordinal 3 起）
    repo = Repository(migrated_db)
    from novelcanon.schemas.ids import alias_fact_id
    from novelcanon.schemas.memory import AliasClaim

    for surface, ordinal in (("小石", 0), ("林风", 3)):
        repo.write_alias(
            AliasClaim(
                alias_fact_id=alias_fact_id(canonical, surface),
                claim_version_id="",
                canonical_id=canonical,
                surface_name=surface,
                observed_ordinal=ordinal,
                observed_chapter_id=ids[ordinal],
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            )
        )

    # display_name 只读 active run：激活该 run
    from novelcanon.schemas.types import RunStatus

    mgr = RunManager(migrated_db)
    assert mgr.transition(run_id, RunStatus.CREATED, RunStatus.RUNNING)
    assert mgr.transition(run_id, RunStatus.RUNNING, RunStatus.VALIDATING)
    assert mgr.transition(run_id, RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE)
    from novelcanon.pipeline.validation import Activator

    assert Activator(migrated_db).activate(run_id) is None

    q = QueryService(migrated_db, book_id)
    # 早期（cutoff=2，前 3 章）：展示小石，不泄露林风
    assert q.display_name(canonical, knowledge_cutoff=2) == "小石"
    # 中期（cutoff=4，前 5 章）：已披露林风 → 林风
    assert q.display_name(canonical, knowledge_cutoff=4) == "林风"
    # 全书：最终名林风
    assert q.display_name(canonical) == "林风"
    # cutoff=-1（第 0 章前）：无 alias 可见 → None（不回退全书最终名）
    assert q.display_name(canonical, knowledge_cutoff=-1) is None


def _mentions_of(migrated_db: Engine) -> list[dict]:
    with migrated_db.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT mention_id, surface_name FROM entity_mentions")
            )
            .mappings()
            .fetchall()
        )
    return [dict(r) for r in rows]


def test_query_scope_expands_mentions(tmp_path, migrated_db: Engine) -> None:
    """查询层 canonical 展开：mention 与 canonical 自身都在作用域内。"""
    book_id, ids, texts = _book_and_chapters(migrated_db, tmp_path)
    run_id = _seed_mentions(migrated_db, book_id, ids, texts)
    service = ResolutionService(migrated_db)
    service.resolve_run(run_id, book_id)

    # 找 萧炎 类 canonical（此处用 seed 后的小石 canonical 演示）
    q = QueryService(migrated_db, book_id)
    mentions = _mentions_of(migrated_db)
    xiaoshi = next(m for m in mentions if m["surface_name"] == "小石")
    with migrated_db.connect() as conn:
        canonical = conn.execute(
            text(
                "SELECT canonical_id FROM entity_resolutions WHERE mention_id = :m"
            ),
            {"m": xiaoshi["mention_id"]},
        ).fetchone()[0]
    scope = q.entity_scope(canonical)
    assert canonical in scope
    # 该 canonical 名下所有 mention 都在作用域
    with migrated_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT mention_id FROM entity_resolutions WHERE canonical_id = :c"
            ),
            {"c": canonical},
        ).fetchall()
    assert {r[0] for r in rows} <= set(scope)
