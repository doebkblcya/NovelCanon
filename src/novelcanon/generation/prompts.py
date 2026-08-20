"""版本化 Map prompt（阶段 06，docs/implementation/06 §1）。

system instruction、few-shot、Schema 分别存储，但共同计算 prompt version：
任一变化都会改变 prompt_version，从而失效 checkpoint（checkpoint 键含
prompt_version / schema_version，见 map_pipeline）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from novelcanon.config.hash import stable_config_hash

DEFAULT_SYSTEM_INSTRUCTION = """\
你是中文长篇小说的结构化信息抽取器。你的任务是从给定的单章正文中抽取本章已披露的事实，\
输出严格符合 Schema 的 JSON。

必须遵守：
1. 只抽取本章正文已明确披露的信息，不得猜测、不得补全、不得把推测写成确定事实。
2. 不得生成 canonical_id、最终证据坐标或跨章事件 ID。
3. 不得引用未提供的未来章节内容（禁止未来结果泄露）。
4. mentions 只记录本章出现的实体提及；mention_id 在本章内唯一。
5. 把握不准的实体提及放入 unresolved，不参与 claims。
6. relation/event/state 的枚举值尽量标准化；无法标准化的原文短语放入 raw 字段。
7. provisional_claims 的 ref_source_segment_id 必须引用下方「可用原文段」中给出的段 ID。
8. ref_source_segments 字段由系统填充，你无需生成内容（输出空数组即可）；
   provisional_claims 的 ref_source_segment_id 引用下方「可用原文段」给出的段 ID。
9. 只输出 JSON 对象本身，不要任何解释或 Markdown 围栏。"""


@dataclass(frozen=True)
class MapPrompts:
    """版本化 Map prompt 三件套（分别存储，共同计算版本）。"""

    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION
    few_shot: list[str] = field(default_factory=list)
    schema_json: str = ""

    def version(self) -> str:
        """prompt_version = hash({system, few_shot, schema})（06 §1）。"""
        return stable_config_hash(
            {
                "system_instruction": self.system_instruction,
                "few_shot": self.few_shot,
                "schema_json": self.schema_json,
            }
        )


def schema_for_draft() -> str:
    """ExtractionDraftV1 的 JSON Schema 文本（排序稳定，供 prompt 与版本化）。"""
    from novelcanon.schemas.draft import ExtractionDraftV1

    return json.dumps(ExtractionDraftV1.model_json_schema(), sort_keys=True, ensure_ascii=False)


def build_map_prompt(
    prompts: MapPrompts,
    chapter_text: str,
    *,
    book_id: str | None = None,
    chapter_id: str | None = None,
    chapter_ordinal: int | None = None,
    chapter_title: str | None = None,
    book_title: str | None = None,
    ref_segment_lines: list[str] | None = None,
    repair_issues: list[str] | None = None,
) -> str:
    """固定排列：固定上下文 → 元数据 → 章节正文 → 可用段 → Schema → 输出指令。

    book_id / chapter_id / chapter_ordinal 为本章输入侧元数据，必须原样填入
    输出 Draft（模型不得自行编造——06 校验层 7 业务不变量会拒绝）。
    repair_issues 非空时附加「上次输出不符合要求」段（结构修复请求，06 §4）。
    """
    parts: list[str] = [f"[系统指令]\n{prompts.system_instruction}"]
    if book_id is not None or chapter_id is not None or chapter_ordinal is not None:
        meta = []
        if book_id is not None:
            meta.append(f"book_id：{book_id}")
        if chapter_id is not None:
            meta.append(f"chapter_id：{chapter_id}")
        if chapter_ordinal is not None:
            meta.append(f"chapter_ordinal：{chapter_ordinal}")
        parts.append(
            "[元数据]（输出 Draft 时必须原样使用下列值，禁止自行填写或留空）\n" + "\n".join(meta)
        )
    if book_title or chapter_title:
        parts.append(
            f"[书籍/章节]\n书：{book_title or '（未知）'}；章：{chapter_title or '（未知）'}"
        )
    parts.append(f"[章节正文]\n{chapter_text}")
    if ref_segment_lines:
        parts.append("[可用原文段]\n" + "\n".join(ref_segment_lines))
    parts.append(f"[输出 Schema]\n{prompts.schema_json}")
    if repair_issues:
        parts.append("[上次输出不符合要求]\n" + "\n".join(f"- {i}" for i in repair_issues))
    parts.append("[输出]\n仅输出符合 Schema 的 JSON 对象，不要任何额外文字。")
    return "\n\n".join(parts)


def default_map_prompts() -> MapPrompts:
    """阶段 06 默认 Map prompt（few-shot 空；Schema 从 Draft 模型导出）。"""
    return MapPrompts(schema_json=schema_for_draft())
