"""数据契约 repository（阶段 02）。

- append-only：claims 历史版本不得更新或删除；update/retract 指向旧版本；
- 幂等：相同 payload 跨 run 复用同一 claim version，只新增 observation；
- 当前版本由查询视图（v_active_claims）推导，不复制可漂移状态；
- 事务边界：每个写入方法一个事务；失败 run 数据可审计但不可见。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from novelcanon.config.hash import stable_config_hash
from novelcanon.schemas.envelope import ClaimEnvelope
from novelcanon.schemas.ids import SCHEMA_VERSION, claim_version_id
from novelcanon.schemas.memory import (
    AliasClaim,
    ClaimPayload,
    EntityRecord,
    EventLinkRecord,
    EvidenceRecord,
)
from novelcanon.schemas.types import Operation, RunStatus


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WriteResult:
    """claim 写入结果：is_new=False 表示幂等命中（仅新增 observation）。"""

    claim_version_id: str
    is_new: bool


class Repository:
    """阶段 02 数据契约写入与查询。single writer 由调用方保证。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── 基础对象：book / chapter / run ─────────────────────────

    def create_book(
        self,
        book_id: str,
        title: str,
        *,
        source_format: str | None = None,
        source_path: str | None = None,
        raw_content_hash: str | None = None,
        normalized_content_hash: str | None = None,
        normalized_text: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO books (book_id, title, source_format, source_path,"
                    " raw_content_hash, normalized_content_hash, normalized_text, created_at)"
                    " VALUES (:id, :title, :fmt, :path, :raw, :norm, :ntext, :ts)"
                ),
                {
                    "id": book_id,
                    "title": title,
                    "fmt": source_format,
                    "path": source_path,
                    "raw": raw_content_hash,
                    "norm": normalized_content_hash,
                    "ntext": normalized_text,
                    "ts": now_iso(),
                },
            )

    def get_book_text(self, book_id: str) -> str:
        """全书规范化文本（索引/证据的基础，SQLite 唯一权威数据源）。"""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT normalized_text FROM books WHERE book_id = :b"), {"b": book_id}
            ).fetchone()
        if row is None or row[0] is None:
            raise ValueError(f"book {book_id} 没有规范化文本，请先执行 import")
        return row[0]

    def create_chapter(
        self,
        chapter_id: str,
        book_id: str,
        ordinal: int,
        *,
        title: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        content_hash: str | None = None,
        volume_id: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO chapters (chapter_id, book_id, ordinal, title,"
                    " char_start, char_end, content_hash, volume_id, created_at)"
                    " VALUES (:id, :book, :ord, :title, :cs, :ce, :hash, :vol, :ts)"
                ),
                {
                    "id": chapter_id,
                    "book": book_id,
                    "ord": ordinal,
                    "title": title,
                    "cs": char_start,
                    "ce": char_end,
                    "hash": content_hash,
                    "vol": volume_id,
                    "ts": now_iso(),
                },
            )

    def create_volume(
        self,
        volume_id: str,
        book_id: str,
        title: str,
        ordinal: int,
        *,
        grouping_version: str = "v1",
        start_chapter_id: str | None = None,
        end_chapter_id: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """创建卷记录（§10：volume_id 持久化 UUID，不由卷序生成）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO volumes (volume_id, book_id, title, ordinal,"
                    " start_chapter_id, end_chapter_id, grouping_version, content_hash, created_at)"
                    " VALUES (:id, :book, :title, :ord, :sc, :ec, :gv, :hash, :ts)"
                ),
                {
                    "id": volume_id,
                    "book": book_id,
                    "title": title,
                    "ord": ordinal,
                    "sc": start_chapter_id,
                    "ec": end_chapter_id,
                    "gv": grouping_version,
                    "hash": content_hash,
                    "ts": now_iso(),
                },
            )

    def start_run(
        self,
        run_id: str,
        book_id: str,
        *,
        pipeline_version: str = "",
        prompt_version: str = "",
        schema_version: str = SCHEMA_VERSION,
        config_hash: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO extraction_runs (run_id, book_id, status,"
                    " pipeline_version, prompt_version, schema_version, config_hash, started_at)"
                    " VALUES (:id, :book, 'running', :pv, :pp, :sv, :cfg, :ts)"
                ),
                {
                    "id": run_id,
                    "book": book_id,
                    "pv": pipeline_version,
                    "pp": prompt_version,
                    "sv": schema_version,
                    "cfg": config_hash,
                    "ts": now_iso(),
                },
            )

    def finish_run(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE extraction_runs SET status = :s, finished_at = :ts, error = :e"
                    " WHERE run_id = :id"
                ),
                {"s": status.value, "ts": now_iso(), "e": error, "id": run_id},
            )

    # ── claims：append-only + 幂等 ─────────────────────────────

    def write_claim(self, envelope: ClaimEnvelope, payload: ClaimPayload) -> WriteResult:
        """写入一条事实版本。

        幂等键 = claim_version_id = f(fact_id, hash({operation, payload}), schema_version)：
        - 已存在 → 仅新增 observation，返回既有版本；
        - 新版本 → 若同 fact 已有旧版本且未显式指定 supersedes，自动指向最新旧版。
        """
        payload_dict = payload.model_dump(mode="json")
        version_key = stable_config_hash(
            {"operation": envelope.operation.value, "payload": payload_dict}
        )
        version_id = claim_version_id(envelope.fact_id, version_key)

        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM claims WHERE claim_version_id = :v"), {"v": version_id}
            ).fetchone()
            if existing:
                self._record_observation(conn, version_id, envelope.created_by_run_id)
                return WriteResult(claim_version_id=version_id, is_new=False)

            supersedes = envelope.supersedes_version_id
            if supersedes is None:
                supersedes = self._latest_version_of_fact(conn, envelope.fact_id, version_id)

            if envelope.operation in (Operation.UPDATE, Operation.RETRACT) and supersedes is None:
                raise ValueError(
                    f"operation={envelope.operation.value} 必须指向已存在版本，"
                    f"但 fact {envelope.fact_id} 无旧版本"
                )

            conn.execute(
                text(
                    "INSERT INTO claims (fact_id, claim_version_id, claim_type, operation,"
                    " supersedes_version_id, confidence, claim_status, observed_chapter_id,"
                    " observed_ordinal, world_valid_kind, world_valid_from, world_valid_to,"
                    " world_valid_confidence, created_by_run_id, prompt_version, pipeline_version,"
                    " created_at, primary_evidence_id)"
                    " VALUES (:fact, :vid, :ctype, :op, :sup, :conf, :status, :och, :oord,"
                    " :wvk, :wfrom, :wto, :wconf, :run, :pv, :pp, :ts, :pev)"
                ),
                {
                    "fact": envelope.fact_id,
                    "vid": version_id,
                    "ctype": envelope.claim_type.value,
                    "op": envelope.operation.value,
                    "sup": supersedes,
                    "conf": envelope.confidence,
                    "status": envelope.claim_status.value,
                    "och": envelope.observed_chapter_id,
                    "oord": envelope.observed_ordinal,
                    "wvk": envelope.world_valid_kind.value,
                    "wfrom": envelope.world_valid_from,
                    "wto": envelope.world_valid_to,
                    "wconf": envelope.world_valid_confidence,
                    "run": envelope.created_by_run_id,
                    "pv": envelope.prompt_version,
                    "pp": envelope.pipeline_version,
                    "ts": envelope.created_at or now_iso(),
                    "pev": envelope.primary_evidence_id,
                },
            )
            self._insert_payload_subtable(conn, version_id, envelope, payload_dict)
            self._record_observation(conn, version_id, envelope.created_by_run_id)
            return WriteResult(claim_version_id=version_id, is_new=True)

    def _insert_payload_subtable(
        self, conn: Connection, version_id: str, envelope: ClaimEnvelope, payload: dict[str, object]
    ) -> None:
        ctype = envelope.claim_type
        if ctype.value == "relation":
            conn.execute(
                text(
                    "INSERT INTO relation_claims (claim_version_id, from_entity_id, to_entity_id,"
                    " relation_type, relation_raw, direction) VALUES (:v, :f, :t, :r, :raw, :d)"
                ),
                {
                    "v": version_id,
                    "f": payload["from_entity_id"],
                    "t": payload["to_entity_id"],
                    "r": payload["relation_type"],
                    "raw": payload.get("relation_raw", ""),
                    "d": payload.get("direction", "undirected"),
                },
            )
        elif ctype.value == "event":
            conn.execute(
                text(
                    "INSERT INTO event_claims (claim_version_id, event_type, summary,"
                    " location_entity_id, sequence_in_chapter, narrative_weight)"
                    " VALUES (:v, :et, :sum, :loc, :seq, :w)"
                ),
                {
                    "v": version_id,
                    "et": payload["event_type"],
                    "sum": payload.get("summary", ""),
                    "loc": payload.get("location_entity_id"),
                    "seq": payload.get("sequence_in_chapter", 0),
                    "w": payload.get("narrative_weight", 0.5),
                },
            )
        elif ctype.value == "state":
            conn.execute(
                text(
                    "INSERT INTO state_claims (claim_version_id, field, value, raw_value,"
                    " subject_entity_id, target_entity_id)"
                    " VALUES (:v, :f, :val, :raw, :subj, :t)"
                ),
                {
                    "v": version_id,
                    "f": payload["field"],
                    "val": payload.get("value"),
                    "raw": payload.get("raw_value"),
                    "subj": payload.get("subject_entity_id"),
                    "t": payload.get("target_entity_id"),
                },
            )
        elif ctype.value == "org":
            conn.execute(
                text(
                    "INSERT INTO org_claims (claim_version_id, org_entity_id, member_entity_id,"
                    " role, action) VALUES (:v, :org, :mem, :role, :act)"
                ),
                {
                    "v": version_id,
                    "org": payload["org_entity_id"],
                    "mem": payload["member_entity_id"],
                    "role": payload.get("role", ""),
                    "act": payload.get("action", "join"),
                },
            )
        elif ctype.value == "foreshadowing":
            conn.execute(
                text(
                    "INSERT INTO foreshadow_claims (claim_version_id, clue_anchor,"
                    " related_entity_ids) VALUES (:v, :anchor, :ids)"
                ),
                {
                    "v": version_id,
                    "anchor": payload["clue_anchor"],
                    "ids": json.dumps(payload.get("related_entity_ids", []), ensure_ascii=False),
                },
            )
        elif ctype.value == "term_definition":
            conn.execute(
                text(
                    "INSERT INTO term_definition_claims (claim_version_id, term_id, definition,"
                    " first_observed_ordinal) VALUES (:v, :term, :def, :ord)"
                ),
                {
                    "v": version_id,
                    "term": payload["term_id"],
                    "def": payload["definition"],
                    "ord": envelope.observed_ordinal,
                },
            )
        else:  # event_link 走独立表
            raise ValueError(f"claim_type {ctype} 应写入 event_links 表")

    def _record_observation(self, conn: Connection, version_id: str, run_id: str) -> None:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO claim_observations (claim_version_id, extraction_run_id,"
                " observed_at) VALUES (:v, :run, :ts)"
            ),
            {"v": version_id, "run": run_id, "ts": now_iso()},
        )

    def _record_alias_observation(self, conn: Connection, version_id: str, run_id: str) -> None:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO alias_observations (claim_version_id, extraction_run_id,"
                " observed_at) VALUES (:v, :run, :ts)"
            ),
            {"v": version_id, "run": run_id, "ts": now_iso()},
        )

    def associate_chapter_products(
        self, conn: Connection, new_run_id: str, source_run_id: str, chapter_id: str
    ) -> None:
        """把某章**来源 run 明确拥有**的产物关联到新 run（checkpoint 复用时，P0）。

        只复制来源 run 成员关系中的 claim/alias/mention，绝不按章节反查全表：
        同章其他 run（如失败 run）的 staging 产物不得随复用进入新 active run。
        mention 通过 mention_observations 复制，entity_mentions.run_id 保持
        首次创建审计，不被改写。
        """
        ts = now_iso()
        conn.execute(
            text(
                "INSERT OR IGNORE INTO claim_observations (claim_version_id, extraction_run_id,"
                " observed_at)"
                " SELECT c.claim_version_id, :new_run, :ts FROM claims c"
                " JOIN claim_observations src ON src.claim_version_id = c.claim_version_id"
                " WHERE src.extraction_run_id = :src AND c.observed_chapter_id = :ch"
            ),
            {"new_run": new_run_id, "ts": ts, "src": source_run_id, "ch": chapter_id},
        )
        conn.execute(
            text(
                "INSERT OR IGNORE INTO alias_observations (claim_version_id, extraction_run_id,"
                " observed_at)"
                " SELECT a.claim_version_id, :new_run, :ts FROM entity_alias_claims a"
                " JOIN alias_observations src ON src.claim_version_id = a.claim_version_id"
                " WHERE src.extraction_run_id = :src AND a.observed_chapter_id = :ch"
            ),
            {"new_run": new_run_id, "ts": ts, "src": source_run_id, "ch": chapter_id},
        )
        conn.execute(
            text(
                "INSERT OR IGNORE INTO mention_observations (mention_id, extraction_run_id,"
                " observed_at)"
                " SELECT m.mention_id, :new_run, :ts FROM entity_mentions m"
                " JOIN mention_observations src ON src.mention_id = m.mention_id"
                " WHERE src.extraction_run_id = :src AND m.chapter_id = :ch"
            ),
            {"new_run": new_run_id, "ts": ts, "src": source_run_id, "ch": chapter_id},
        )

    def ensure_run_belongs_to_book(self, run_id: str, book_id: str) -> None:
        """写入边界校验：run 必须属于该书（materialize 入口，P1）。"""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT book_id FROM extraction_runs WHERE run_id = :r"), {"r": run_id}
            ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} 不存在")
        if row[0] != book_id:
            raise ValueError(f"run {run_id} 属于 book {row[0]}，不能用于 book {book_id}")

    def ensure_chapter_belongs_to_book(self, chapter_id: str, book_id: str) -> None:
        """写入边界校验：章节必须属于该书（materialize 入口，P1）。"""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT book_id FROM chapters WHERE chapter_id = :c"), {"c": chapter_id}
            ).fetchone()
        if row is None:
            raise ValueError(f"chapter {chapter_id} 不存在")
        if row[0] != book_id:
            raise ValueError(f"chapter {chapter_id} 属于 book {row[0]}，不能用于 book {book_id}")

    @staticmethod
    def _latest_version_of_fact(
        conn: Connection, fact_id: str, exclude_version_id: str
    ) -> str | None:
        row = conn.execute(
            text(
                "SELECT claim_version_id FROM claims WHERE fact_id = :f"
                " AND claim_version_id != :v ORDER BY rowid DESC LIMIT 1"
            ),
            {"f": fact_id, "v": exclude_version_id},
        ).fetchone()
        return row[0] if row else None

    # ── event_link（一等事实表）────────────────────────────────

    def write_event_link(self, record: EventLinkRecord) -> WriteResult:
        payload = record.payload.model_dump(mode="json")
        version_key = stable_config_hash(
            {"operation": record.envelope.operation.value, "payload": payload}
        )
        version_id = claim_version_id(record.envelope.fact_id, version_key)
        verification_evidence = record.verification_evidence
        verification_method = record.verification_method
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM event_links WHERE claim_version_id = :v"), {"v": version_id}
            ).fetchone()
            if existing:
                # 幂等命中：复用版本，仍把该 run 加入成员关系（09 §4）；
                # 若本轮关系证据验证通过，把边提升为 supported 并记录验证
                # 信息（P0：验证是后续语义步骤，重跑可补验/覆盖）。
                if verification_method is not None:
                    conn.execute(
                        text(
                            "UPDATE event_links SET claim_status = 'supported',"
                            " verification_method = :vm, verification_evidence = :ve"
                            " WHERE claim_version_id = :v"
                        ),
                        {
                            "vm": verification_method,
                            "ve": verification_evidence,
                            "v": version_id,
                        },
                    )
                self._record_event_link_observation(
                    conn, version_id, record.envelope.created_by_run_id
                )
                return WriteResult(claim_version_id=version_id, is_new=False)
            supersedes = record.envelope.supersedes_version_id
            if supersedes is None:
                row = conn.execute(
                    text(
                        "SELECT claim_version_id FROM event_links WHERE fact_id = :f"
                        " AND claim_version_id != :v ORDER BY rowid DESC LIMIT 1"
                    ),
                    {"f": record.envelope.fact_id, "v": version_id},
                ).fetchone()
                supersedes = row[0] if row else None
            conn.execute(
                text(
                    "INSERT INTO event_links (claim_version_id, fact_id, source_event_id,"
                    " target_event_id, relation_type, confidence, claim_status,"
                    " observed_chapter_id,"
                    " observed_ordinal, supersedes_version_id, primary_evidence_id,"
                    " created_by_run_id, verification_method, verification_evidence)"
                    " VALUES (:v, :fact, :src, :tgt, :rt, :conf, :status, :och, :oord,"
                    " :sup, :pev, :run, :vm, :ve)"
                ),
                {
                    "v": version_id,
                    "fact": record.envelope.fact_id,
                    "src": payload["source_event_id"],
                    "tgt": payload["target_event_id"],
                    "rt": payload["relation_type"],
                    "conf": record.envelope.confidence,
                    "status": record.envelope.claim_status.value,
                    "och": record.envelope.observed_chapter_id,
                    "oord": record.envelope.observed_ordinal,
                    "sup": supersedes,
                    "pev": record.envelope.primary_evidence_id,
                    "run": record.envelope.created_by_run_id,
                    "vm": verification_method,
                    "ve": verification_evidence,
                },
            )
            self._record_event_link_observation(
                conn, version_id, record.envelope.created_by_run_id
            )
            return WriteResult(claim_version_id=version_id, is_new=True)

    def _record_event_link_observation(
        self, conn: Connection, version_id: str, run_id: str
    ) -> None:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO event_link_observations (claim_version_id,"
                " extraction_run_id, observed_at) VALUES (:v, :run, :ts)"
            ),
            {"v": version_id, "run": run_id, "ts": now_iso()},
        )

    # ── evidence ───────────────────────────────────────────────

    def write_evidence(self, evidence: EvidenceRecord) -> bool:
        """写证据；同 claim version + 同 span 幂等（唯一约束）。返回是否新增。"""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "INSERT OR IGNORE INTO claim_evidence (evidence_id, claim_version_id,"
                    " evidence_stance, evidence_type, chapter_id, char_start, char_end, span_hash,"
                    " literal_match_rate, verification_method, verification_run_id)"
                    " VALUES (:eid, :vid, :stance, :etype, :ch, :cs, :ce, :hash, :rate, :vm, :vr)"
                ),
                {
                    "eid": evidence.evidence_id,
                    "vid": evidence.claim_version_id,
                    "stance": evidence.evidence_stance.value,
                    "etype": evidence.evidence_type.value,
                    "ch": evidence.chapter_id,
                    "cs": evidence.char_start,
                    "ce": evidence.char_end,
                    "hash": evidence.span_hash,
                    "rate": evidence.literal_match_rate,
                    "vm": evidence.verification_method,
                    "vr": evidence.verification_run_id,
                },
            )
            return result.rowcount == 1

    def add_event_participant(
        self, event_claim_version_id: str, entity_id: str, role: str = ""
    ) -> None:
        """写入 event_participants（事件参与者只存关联表，§5.2）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO event_participants"
                    " (event_claim_version_id, entity_id, role)"
                    " VALUES (:e, :ent, :role)"
                ),
                {"e": event_claim_version_id, "ent": entity_id, "role": role},
            )

    def set_claim_status(self, claim_version_id_value: str, status: str) -> None:
        """聚合后回写 claim 状态（§6：supports/refutes 聚合结果）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE claims SET claim_status = :s WHERE claim_version_id = :v"),
                {"s": status, "v": claim_version_id_value},
            )

    def ensure_term(self, term_id: str, chapter_id: str, ordinal: int) -> None:
        """确保术语存在（terms 表，term_definition claim 的 FK 前置，阶段 07）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO terms (term_id, canonical_name,"
                    " first_observed_ordinal, created_at)"
                    " VALUES (:t, :name, :ord, :ts)"
                ),
                {
                    "t": term_id,
                    "name": term_id,
                    "ord": ordinal,
                    "ts": now_iso(),
                },
            )

    def set_primary_evidence(self, claim_version_id_value: str, evidence_id_value: str) -> None:
        """设置 primary_evidence_id（只用于查询加速，07 §5）。"""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE claims SET primary_evidence_id = :e"
                    " WHERE claim_version_id = :v"
                ),
                {"e": evidence_id_value, "v": claim_version_id_value},
            )

    # ── entities / aliases / mentions / merge ──────────────────

    def upsert_entity(self, entity: EntityRecord) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entities (canonical_id, canonical_name, tier, importance_score,"
                    " created_by_run_id, created_at) VALUES (:id, :name, :tier, :score, :run, :ts)"
                    " ON CONFLICT(canonical_id) DO UPDATE SET canonical_name ="
                    " excluded.canonical_name,"
                    " tier = excluded.tier, importance_score = excluded.importance_score"
                ),
                {
                    "id": entity.canonical_id,
                    "name": entity.canonical_name,
                    "tier": entity.tier.value,
                    "score": entity.importance_score,
                    "run": entity.created_by_run_id,
                    "ts": now_iso(),
                },
            )

    def write_alias(self, alias: AliasClaim) -> WriteResult:
        """别名披露写入（幂等：同 alias_fact + 同 payload 复用版本）。"""
        version_key = stable_config_hash(
            {"operation": alias.operation.value, "surface_name": alias.surface_name}
        )
        version_id = claim_version_id(alias.alias_fact_id, version_key)
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM entity_alias_claims WHERE claim_version_id = :v"),
                {"v": version_id},
            ).fetchone()
            if existing:
                # 幂等命中：复用版本，但仍需把该 run 加入别名成员关系
                self._record_alias_observation(conn, version_id, alias.created_by_run_id)
                return WriteResult(claim_version_id=version_id, is_new=False)
            supersedes = alias.supersedes_version_id
            if supersedes is None:
                row = conn.execute(
                    text(
                        "SELECT claim_version_id FROM entity_alias_claims WHERE alias_fact_id = :f"
                        " AND claim_version_id != :v ORDER BY rowid DESC LIMIT 1"
                    ),
                    {"f": alias.alias_fact_id, "v": version_id},
                ).fetchone()
                supersedes = row[0] if row else None
            conn.execute(
                text(
                    "INSERT INTO entity_alias_claims (claim_version_id, alias_fact_id,"
                    " canonical_id,"
                    " surface_name, operation, supersedes_version_id, observed_ordinal,"
                    " observed_chapter_id, created_by_run_id, created_at)"
                    " VALUES (:v, :fact, :canon, :name, :op, :sup, :oord, :och, :run, :ts)"
                ),
                {
                    "v": version_id,
                    "fact": alias.alias_fact_id,
                    "canon": alias.canonical_id,
                    "name": alias.surface_name,
                    "op": alias.operation.value,
                    "sup": supersedes,
                    "oord": alias.observed_ordinal,
                    "och": alias.observed_chapter_id,
                    "run": alias.created_by_run_id,
                    "ts": alias.created_at or now_iso(),
                },
            )
            self._record_alias_observation(conn, version_id, alias.created_by_run_id)
            return WriteResult(claim_version_id=version_id, is_new=True)

    def write_mention(
        self,
        mention_id: str,
        chapter_id: str,
        surface_name: str,
        run_id: str,
        *,
        char_start: int | None = None,
        char_end: int | None = None,
        canonical_id: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entity_mentions (mention_id, chapter_id, surface_name,"
                    " char_start, char_end, canonical_id, run_id, created_at)"
                    " VALUES (:m, :ch, :name, :cs, :ce, :canon, :run, :ts)"
                    " ON CONFLICT(mention_id) DO UPDATE SET"
                    "   char_start = excluded.char_start, char_end = excluded.char_end"
                ),
                {
                    "m": mention_id,
                    "ch": chapter_id,
                    "name": surface_name,
                    "cs": char_start,
                    "ce": char_end,
                    "canon": canonical_id,
                    "run": run_id,
                    "ts": now_iso(),
                },
            )
            # mention 成员关系（run_id 列保持首次创建审计，成员关系走 observations）
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO mention_observations (mention_id, extraction_run_id,"
                    " observed_at) VALUES (:m, :run, :ts)"
                ),
                {"m": mention_id, "run": run_id, "ts": now_iso()},
            )

    def record_merge(
        self,
        action: str,
        from_entity_id: str,
        to_entity_id: str,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entity_merge_audit (action, from_entity_id, to_entity_id, reason,"
                    " run_id, created_at) VALUES (:a, :f, :t, :r, :run, :ts)"
                ),
                {
                    "a": action,
                    "f": from_entity_id,
                    "t": to_entity_id,
                    "r": reason,
                    "run": run_id,
                    "ts": now_iso(),
                },
            )

    # ── evidence errors（阶段 07：对齐失败审计）─────────────────

    def write_evidence_error(
        self,
        *,
        error_id: str,
        run_id: str,
        book_id: str,
        chapter_id: str,
        claim_id: str = "",
        ref_segment_id: str = "",
        stage: str,
        error_code: str,
        message: str,
    ) -> bool:
        """写入一条证据对齐错误（幂等：唯一约束 + INSERT OR IGNORE）。"""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "INSERT OR IGNORE INTO evidence_errors (error_id, run_id, book_id,"
                    " chapter_id, claim_id, ref_segment_id, stage, error_code, message,"
                    " created_at) VALUES (:eid, :run, :book, :ch, :cid, :ref, :st, :code,"
                    " :msg, :ts)"
                ),
                {
                    "eid": error_id,
                    "run": run_id,
                    "book": book_id,
                    "ch": chapter_id,
                    "cid": claim_id,
                    "ref": ref_segment_id,
                    "st": stage,
                    "code": error_code,
                    "msg": message,
                    "ts": now_iso(),
                },
            )
            return result.rowcount == 1

    def list_evidence_errors(self, run_id: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT * FROM evidence_errors WHERE run_id = :r"
                        " ORDER BY chapter_id, claim_id"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    # ── staging（阶段 07：读取 Map Draft 做证据对齐）────────────

    def list_valid_map_drafts(self, run_id: str) -> list[dict]:
        """某 run 全部 valid Draft（draft_json + 章定位）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT draft_id, run_id, book_id, chapter_id, ordinal,"
                        " content_hash, draft_json FROM map_drafts"
                        " WHERE run_id = :r AND status = 'valid' ORDER BY ordinal"
                    ),
                    {"r": run_id},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def chapter_text_for(self, book_id: str, chapter_id: str) -> str:
        """某章规范化正文（book 全文 + 章区间）。"""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT char_start, char_end FROM chapters WHERE chapter_id = :c"
                    " AND book_id = :b"
                ),
                {"c": chapter_id, "b": book_id},
            ).fetchone()
        if row is None:
            raise ValueError(f"chapter {chapter_id} 不存在（book {book_id}）")
        full = self.get_book_text(book_id)
        return full[row[0] : row[1]]

    # ── 查询（默认只读 active run，阶段 02 视图）────────────────

    def list_active_runs(self, book_id: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT run_id FROM extraction_runs WHERE book_id = :b AND status = 'active'"),
                {"b": book_id},
            ).fetchall()
            return [r[0] for r in rows]

    def current_version(self, fact_id: str) -> dict | None:
        """active run 中某 fact 的当前版本。

        取每个 fact 的**最新**版本；若最新版本是 retract（事实已被撤回），
        则该 fact 无当前版本（不回溯到旧版）。
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM ("
                        "  SELECT c.*, ROW_NUMBER() OVER (PARTITION BY fact_id"
                        " ORDER BY _rowid DESC) AS rn"
                        "  FROM v_active_claims c WHERE fact_id = :f"
                        ") WHERE rn = 1 AND operation != 'retract'"
                    ),
                    {"f": fact_id},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def get_claim(self, claim_version_id_value: str) -> dict | None:
        """按版本 ID 读取一条 claim（含未激活 run，审计用）。"""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM claims WHERE claim_version_id = :v"),
                    {"v": claim_version_id_value},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def observations_for(self, claim_version_id_value: str) -> list[dict]:
        """某版本的观察记录（幂等验收：重复 run 只新增 observation）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT * FROM claim_observations WHERE claim_version_id = :v"),
                    {"v": claim_version_id_value},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def active_claims_for_book(self, book_id: str) -> list[dict]:
        """某本书 active run 的 claim（视图带 book_id 列，多书隔离）。"""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT c.* FROM v_active_claims c WHERE c.book_id = :b"),
                    {"b": book_id},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def list_entities(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(text("SELECT * FROM entities ORDER BY canonical_id"))
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def get_entity(self, canonical_id: str) -> dict | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM entities WHERE canonical_id = :id"),
                    {"id": canonical_id},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None

    def list_chapters(self, book_id: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT * FROM chapters WHERE book_id = :b ORDER BY ordinal"),
                    {"b": book_id},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def merge_audit(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(text("SELECT * FROM entity_merge_audit ORDER BY audit_id"))
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def evidence_for(self, claim_version_id_value: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text("SELECT * FROM claim_evidence WHERE claim_version_id = :v"),
                    {"v": claim_version_id_value},
                )
                .mappings()
                .fetchall()
            )
            return [dict(r) for r in rows]

    def foreign_key_check(self) -> list[dict]:
        """PRAGMA foreign_key_check：验收要求无错误。"""
        with self._engine.connect() as conn:
            rows = conn.execute(text("PRAGMA foreign_key_check")).mappings().fetchall()
            return [dict(r) for r in rows]
