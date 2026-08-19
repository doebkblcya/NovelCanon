"""公共 claim envelope（定版方案 §4.2）。

事实版本 append-only；update/retract 经 supersedes_version_id 连接旧版本；
当前版本由查询视图推导，不复制可漂移状态。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from novelcanon.schemas.types import ClaimStatus, ClaimType, Operation, WorldValidKind


class ClaimEnvelope(BaseModel):
    """所有事实类型的公共字段。类型专属字段存入一对一子表（§4.2）。"""

    fact_id: str
    claim_version_id: str
    claim_type: ClaimType
    operation: Operation
    supersedes_version_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    claim_status: ClaimStatus = ClaimStatus.UNVERIFIED
    observed_chapter_id: str | None = None
    observed_ordinal: int | None = None
    world_valid_kind: WorldValidKind = WorldValidKind.UNKNOWN
    world_valid_from: int | None = None
    world_valid_to: int | None = None
    world_valid_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    created_by_run_id: str
    prompt_version: str = ""
    pipeline_version: str = ""
    created_at: str
    primary_evidence_id: str | None = None


class ClaimObservation(BaseModel):
    """claim_observations：某次 run 观察到的既有版本（§4.3）。"""

    claim_version_id: str
    extraction_run_id: str
    observed_at: str
