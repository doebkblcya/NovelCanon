"""ontology 与 state catalog（定版方案 §5.1/§5.4）。

关系/事件类型来自受控本体，禁止自由文本直接进入标准字段；
状态字段由 state catalog 约束，扩展枚举须更新 catalog 版本并迁移。
初始版本种子同时写入 migration（阶段 02），此处为唯一权威定义。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from novelcanon.schemas.types import ClaimType


class StateCatalogEntry(BaseModel):
    """state catalog 行（§5.4）。"""

    field: str
    value_type: str
    multivalued: bool = False
    target_required: bool = False
    enum_values: list[str] = Field(default_factory=list)
    unit: str | None = None
    sort_values: list[str] = Field(default_factory=list)
    applicable_entity_types: list[str] = Field(default_factory=list)
    catalog_version: str = "v1"


class OntologyVersion(BaseModel):
    """ontology_versions 行：受控本体值域版本。"""

    version: str
    claim_type: ClaimType
    allowed_values: list[str]
    schema_version: str = "v1"


RELATION_ONTOLOGY_V1 = [
    "师徒",
    "夫妻",
    "父子",
    "母子",
    "父女",
    "母女",
    "兄弟",
    "姐妹",
    "挚友",
    "恋人",
    "敌人",
    "盟友",
    "同门",
    "主仆",
    "上下级",
    "同族",
    "同乡",
    "同窗",
    "师兄弟",
    "宿敌",
]

EVENT_ONTOLOGY_V1 = [
    "战斗",
    "对决",
    "修炼",
    "突破",
    "晋升",
    "拜师",
    "结盟",
    "背叛",
    "死亡",
    "重生",
    "寻宝",
    "伏击",
    "交谈",
    "聚会",
    "告密",
    "求婚",
    "成婚",
    "生子",
    "分离",
    "重逢",
    "追杀",
    "逃亡",
    "审讯",
    "招揽",
    "辞别",
    "传功",
    "炼制",
    "拍卖",
    "比试",
]

STATE_CATALOG_V1: list[StateCatalogEntry] = [
    StateCatalogEntry(
        field="stance",
        value_type="relation_stance",
        target_required=True,
        enum_values=["友好", "敌对", "中立", "师徒", "主仆", "爱慕", "仇恨"],
    ),
    StateCatalogEntry(
        field="cultivation_realm",
        value_type="ordered_enum",
        enum_values=["炼气", "筑基", "金丹", "元婴", "化神", "合体", "大乘", "渡劫"],
        sort_values=["炼气", "筑基", "金丹", "元婴", "化神", "合体", "大乘", "渡劫"],
    ),
    StateCatalogEntry(field="alive", value_type="bool"),
    StateCatalogEntry(field="identity", value_type="canonical_ref"),
    StateCatalogEntry(field="location", value_type="canonical_ref"),
    StateCatalogEntry(
        field="possession", value_type="canonical_ref", multivalued=True, target_required=True
    ),
]


def state_catalog_entries() -> list[StateCatalogEntry]:
    return STATE_CATALOG_V1
