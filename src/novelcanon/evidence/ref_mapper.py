"""ref 回映射器（阶段 07，docs/implementation/07 §1）。

无压缩时，ref_source_segment 直接指向原文区间：segment_id + 章内
char_offset + segment_content_hash。回映射器按 segment_id 把 Draft 的
ref_source_segment 还原为本章规范化原文的 [char_start, char_end) 半开区间。

原理（07 §1「无压缩时 ref 直接映射到原文章节和半开区间」）：
- pipeline 注入的 ref_source_segments 是按 split_for_window 切分生成的
  完整段清单（无重叠、无间隔，stage 06 默认 overlap=0），因此按
  char_offset 排序后：段 i 的终点 = 段 i+1 的起点，最后一段终点 = 章长；
- 每段的 segment_content_hash 必须与原文切片的 hash 完全一致（100% 复现）。

验证链（§1 强制）：
- 段 ID 必须存在于注入清单内（否则 ref_missing）；
- char_offset 必须落在章内（否则 ref_out_of_range）；
- 原文切片 hash 必须与 segment_content_hash 一致（否则 ref_hash_mismatch）。

任一级失败都抛出 RefMappingError（含稳定 error_code），由 service 写入
evidence_errors（staging/error），不允许猜测修复后直接激活。
"""

from __future__ import annotations

from dataclasses import dataclass

from novelcanon.ingestion.normalize import sha256
from novelcanon.schemas.draft import RefSourceSegment


class RefMappingError(Exception):
    """ref 回映射失败（对应 evidence_errors.stage='ref_mapping'）。

    error_code 为稳定分类：ref_missing / ref_out_of_range / ref_hash_mismatch。
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class MappedSpan:
    """ref_source_segment 回映射结果：一段规范化原文区间。"""

    segment_id: str
    chapter_id: str
    char_start: int
    char_end: int
    text: str


class RefMapper:
    """把 Draft 的 ref_source_segments 回映射为原文 span（hash 验证）。

    段清单有序且连续（pipeline 注入），相邻 offset 推断终点后逐段
    hash 验证；任一段失败即整体失败（该章对齐中止，进错误表）。
    """

    def __init__(self, chapter_id: str, chapter_text: str) -> None:
        self._chapter_id = chapter_id
        self._chapter_text = chapter_text

    def map(self, refs: list[RefSourceSegment]) -> dict[str, MappedSpan]:
        """映射全部 ref_source_segment；任一段失败即抛 RefMappingError。

        返回 segment_id -> MappedSpan。
        """
        if not refs:
            raise RefMappingError("ref_missing", "Draft 无 ref_source_segments（无法定位证据）")
        ordered = sorted(refs, key=lambda r: r.char_offset)
        text_len = len(self._chapter_text)
        result: dict[str, MappedSpan] = {}
        for i, ref in enumerate(ordered):
            if not (0 <= ref.char_offset <= text_len):
                raise RefMappingError(
                    "ref_out_of_range",
                    f"段 {ref.segment_id} char_offset={ref.char_offset} 越界"
                    f"（章长 {text_len}）",
                )
            end = ordered[i + 1].char_offset if i + 1 < len(ordered) else text_len
            span_text = self._chapter_text[ref.char_offset:end]
            if sha256(span_text) != ref.segment_content_hash:
                raise RefMappingError(
                    "ref_hash_mismatch",
                    f"段 {ref.segment_id} 原文切片 hash 与注入清单不一致"
                    f"（[{ref.char_offset},{end})）",
                )
            result[ref.segment_id] = MappedSpan(
                segment_id=ref.segment_id,
                chapter_id=self._chapter_id,
                char_start=ref.char_offset,
                char_end=end,
                text=span_text,
            )
        return result
