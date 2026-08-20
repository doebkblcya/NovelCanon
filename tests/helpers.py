"""阶段 03 测试工具：确定性 fixture EPUB 生成与黄金文本。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sqlalchemy import Engine

# (章标题, 正文) —— 含黄金专名/原句（FTS 召回测试用）
FIXTURE_CHAPTERS: list[tuple[str, str]] = [
    (
        "第一章 雨夜惊变",
        "林风在雨夜中踏入青云宗的山门，看见一块巨大的石碑，上面写着「青云不朽」四字。"
        "守门的弟子打量了他一眼，冷声道：青云宗不收来历不明之人。",
    ),
    (
        "第二章 三年之约",
        "萧炎与纳兰嫣然在乌坦城定下三年之约，围观者议论纷纷。"
        "少女回身离去时，留下一句：三年后，若你还是废物，便休要再提婚约。",
    ),
    (
        "第三章 异火榜",
        "药老提起异火榜，萧炎的目光落在青莲地心火上。"
        "异火乃是天地奇物，得之可炼万物，亦可焚尽苍穹。",
    ),
]


def make_fixture_epub(path: Path, chapters: list[tuple[str, str]], title: str = "测试小说") -> None:
    """生成一个最小合法 EPUB2（container/opf/ncx/xhtml），用于确定性测试。"""
    n = len(chapters)
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles>"
        '<rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    manifest = "".join(
        f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(n)
    )
    spine = "".join(f'<itemref idref="ch{i}"/>' for i in range(n))
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>"
        '<dc:identifier id="uid">fixture-001</dc:identifier>'
        "</metadata>"
        "<manifest>"
        f'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{manifest}'
        "</manifest>"
        f'<spine toc="ncx">{spine}</spine>'
        "</package>"
    )
    nav_points = "".join(
        f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{t}</text></navLabel>'
        f'<content src="ch{i}.xhtml"/></navPoint>'
        for i, (t, _) in enumerate(chapters)
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{nav_points}</navMap></ncx>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        for i, (t, body) in enumerate(chapters):
            xhtml = (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"<h1>{t}</h1><p>{body}</p>"
                "</body></html>"
            )
            z.writestr(f"OEBPS/ch{i}.xhtml", xhtml)


# ── 阶段 10 测试数据：带 active claims 的导入书 ────────────────


def seed_active_book(engine: Engine, tmp_path: Path, book_id: str = "book_s10") -> dict:
    """导入 3 章 fixture EPUB + 写 state/relation/event/org/term claims
    + alias + 证据，并把 run 激活。返回测试用字典（book_id/run_id/
    chapters/canonical/claim 映射等）。

    覆盖 10 阶段各查询路线所需的最小数据集：
    - 实体：林风（青云宗弟子）、萧炎（乌坦城、萧家少主）、药老（萧炎
      师父）、纳兰嫣然（萧炎恋人）；
    - 事件：踏入山门(ch1) / 定下三年之约(ch2) / 异火榜(ch3)；
    - 术语：异火定义(ch3)。
    """
    from novelcanon.ingestion.service import import_book
    from novelcanon.pipeline import RunManager
    from novelcanon.pipeline.validation import Activator
    from novelcanon.schemas.envelope import ClaimEnvelope
    from novelcanon.schemas.ids import alias_fact_id, evidence_id, state_fact_id
    from novelcanon.schemas.memory import (
        AliasClaim,
        EntityRecord,
        EvidenceRecord,
    )
    from novelcanon.schemas.payloads import (
        EventPayload,
        OrgPayload,
        RelationPayload,
        StatePayload,
        TermDefinitionPayload,
    )
    from novelcanon.schemas.types import (
        ClaimStatus,
        EntityTier,
        EvidenceStance,
        EvidenceType,
        Operation,
        RunStatus,
    )
    from novelcanon.storage.repository import Repository

    epub = tmp_path / f"{book_id}.epub"
    make_fixture_epub(epub, FIXTURE_CHAPTERS, title="阶段10测试")
    import_book(engine, epub, book_id=book_id)
    repo = Repository(engine)
    chapters = repo.list_chapters(book_id)
    ids = {c["ordinal"]: c["chapter_id"] for c in chapters}
    full = repo.get_book_text(book_id)
    texts = {c["ordinal"]: full[c["char_start"] : c["char_end"]] for c in chapters}

    run_id = RunManager(engine).create(book_id, input_hash="s10-fixture")
    entities = {
        "linfeng": ("ent_linfeng", "林风"),
        "qingyunzong": ("ent_qingyunzong", "青云宗"),
        "xiaoyan": ("ent_xiaoyan", "萧炎"),
        "yaolao": ("ent_yaolao", "药老"),
        "nalan": ("ent_nalan", "纳兰嫣然"),
        "xiaojia": ("ent_xiaojia", "萧家"),
        "wutancheng": ("ent_wutancheng", "乌坦城"),
    }
    for key, (cid, name) in entities.items():
        repo.upsert_entity(
            EntityRecord(
                canonical_id=cid,
                canonical_name=name,
                tier=EntityTier.CORE if key in ("linfeng", "xiaoyan") else EntityTier.MAJOR,
                created_by_run_id=run_id,
            )
        )

    claims: dict[str, str] = {}  # 标签 → claim_version_id

    def _write(label: str, ctype: str, payload, ordinal: int, fact_id: str) -> str:
        result = repo.write_claim(
            ClaimEnvelope(
                fact_id=fact_id,
                claim_version_id="",
                claim_type=ctype,
                operation=Operation.ASSERT,
                claim_status=ClaimStatus.SUPPORTED,
                observed_chapter_id=ids[ordinal],
                observed_ordinal=ordinal,
                # 双时间查询要求世界有效元数据：章节近似（chapter_proxy）
                world_valid_kind="chapter_proxy",
                world_valid_from=ordinal,
                world_valid_to=None,
                world_valid_confidence=1.0,
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload,
        )
        claims[label] = result.claim_version_id
        _evidence(result.claim_version_id, ordinal, texts[ordinal])
        return result.claim_version_id

    def _evidence(version_id: str, ordinal: int, ch_text: str) -> None:
        span = ch_text[: min(20, len(ch_text))]
        from novelcanon.ingestion.normalize import sha256 as h

        eid = evidence_id(version_id, ids[ordinal], 0, len(span), h(span))
        repo.write_evidence(
            EvidenceRecord(
                evidence_id=eid,
                claim_version_id=version_id,
                evidence_stance=EvidenceStance.SUPPORTS,
                evidence_type=EvidenceType.DIRECT,
                chapter_id=ids[ordinal],
                char_start=0,
                char_end=len(span),
                span_hash=h(span),
                literal_match_rate=1.0,
                verification_method="hash-exact",
            )
        )

    # state claims
    from novelcanon.schemas.ids import (
        event_fact_id,
        org_fact_id,
        relation_fact_id,
        term_definition_fact_id,
    )

    _write(
        "state_location_linfeng",
        "state",
        StatePayload(
            field="location",
            value="青云宗",
            subject_entity_id="ent_linfeng",
        ),
        0,
        state_fact_id("ent_linfeng", "location"),
    )
    _write(
        "state_alive_xiaoyan",
        "state",
        StatePayload(field="alive", value="true", subject_entity_id="ent_xiaoyan"),
        1,
        state_fact_id("ent_xiaoyan", "alive"),
    )
    # relation claims
    _write(
        "relation_mentor",
        "relation",
        RelationPayload(
            from_entity_id="ent_yaolao",
            to_entity_id="ent_xiaoyan",
            relation_type="师徒",
            relation_raw="药老收萧炎为弟子",
        ),
        1,
        relation_fact_id("ent_yaolao", "师徒", "ent_xiaoyan"),
    )
    _write(
        "relation_lovers",
        "relation",
        RelationPayload(
            from_entity_id="ent_xiaoyan",
            to_entity_id="ent_nalan",
            relation_type="恋人",
            relation_raw="定下三年之约",
        ),
        1,
        relation_fact_id("ent_xiaoyan", "恋人", "ent_nalan"),
    )
    # event claims（带 participants）
    ev_linfeng = _write(
        "event_linfeng",
        "event",
        EventPayload(
            event_type="拜师",
            summary="林风拜入青云宗",
            location_entity_id="ent_qingyunzong",
            sequence_in_chapter=1,
        ),
        0,
        event_fact_id("拜师", ["ent_linfeng", "ent_qingyunzong"], "ent_qingyunzong", ids[0], 1),
    )
    repo.add_event_participant(ev_linfeng, "ent_linfeng")
    repo.add_event_participant(ev_linfeng, "ent_qingyunzong")
    ev_promise = _write(
        "event_promise",
        "event",
        EventPayload(
            event_type="定约",
            summary="萧炎与纳兰嫣然定下三年之约",
            location_entity_id="ent_wutancheng",
            sequence_in_chapter=1,
        ),
        1,
        event_fact_id("定约", ["ent_xiaoyan", "ent_nalan"], "ent_wutancheng", ids[1], 1),
    )
    repo.add_event_participant(ev_promise, "ent_xiaoyan")
    repo.add_event_participant(ev_promise, "ent_nalan")
    # org claims
    _write(
        "org_xiaoyan_xiaojia",
        "org",
        OrgPayload(
            org_entity_id="ent_xiaojia",
            member_entity_id="ent_xiaoyan",
            role="少主",
            action="join",
        ),
        1,
        org_fact_id("ent_xiaojia", "ent_xiaoyan", "少主"),
    )
    _write(
        "org_linfeng_zong",
        "org",
        OrgPayload(
            org_entity_id="ent_qingyunzong",
            member_entity_id="ent_linfeng",
            role="弟子",
            action="join",
        ),
        0,
        org_fact_id("ent_qingyunzong", "ent_linfeng", "弟子"),
    )
    # term definition（第 3 章「异火」）
    repo.ensure_term("异火", ids[2], 2)
    _write(
        "term_yihuo",
        "term_definition",
        TermDefinitionPayload(
            term_id="异火",
            definition="天地奇物，得之可炼万物，亦可焚尽苍穹",
        ),
        2,
        term_definition_fact_id("异火"),
    )
    # alias（surface → canonical，供实体解析/展示名）
    for _, (cid, name) in entities.items():
        repo.write_alias(
            AliasClaim(
                alias_fact_id=alias_fact_id(cid, name),
                claim_version_id="",
                canonical_id=cid,
                surface_name=name,
                observed_ordinal=0,
                observed_chapter_id=ids[0],
                created_by_run_id=run_id,
                created_at="2026-01-01T00:00:00+00:00",
            )
        )

    # 激活 run（created → running → validating → ready_to_activate → active）
    mgr = RunManager(engine)
    for f, t in (
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.VALIDATING),
        (RunStatus.VALIDATING, RunStatus.READY_TO_ACTIVATE),
    ):
        assert mgr.transition(run_id, f, t)
    assert Activator(engine).activate(run_id) is None

    return {
        "book_id": book_id,
        "run_id": run_id,
        "chapters": ids,
        "texts": texts,
        "entities": {k: v[0] for k, v in entities.items()},
        "entity_names": {k: v[1] for k, v in entities.items()},
        "claims": claims,
    }
