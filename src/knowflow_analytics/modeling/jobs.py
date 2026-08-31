from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from knowflow_analytics.contracts import FrozenModel


class ModelingJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelingJobStage(StrEnum):
    """与 create_ai_modeling_proposal 的两个 LLM 扇出阶段一一对应。"""

    QUEUED = "queued"
    MODELING = "modeling"  # 每表一次 ModelSchema
    ENRICHING = "enriching"  # 别名 / 计数指标 / 分析主题
    DONE = "done"


TableStatus = Literal["pending", "running", "completed", "failed"]


class ModelingJobTable(FrozenModel):
    model_id: str
    name: str
    status: TableStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=1_000)


class ModelingJobProgress(FrozenModel):
    tables: tuple[ModelingJobTable, ...] = ()

    @property
    def done(self) -> int:
        return sum(item.status in {"completed", "failed"} for item in self.tables)

    @property
    def total(self) -> int:
        return len(self.tables)

    def with_table(self, model_id: str, **changes) -> ModelingJobProgress:
        return self.model_copy(
            update={
                "tables": tuple(
                    item.model_copy(update=changes) if item.model_id == model_id else item
                    for item in self.tables
                )
            }
        )


class ModelingJob(FrozenModel):
    """一次 AI 建模的执行记录。

    此前整条链路是同步的：一次 HTTP 请求跑完两个 LLM 扇出阶段，唯一的 DB 写在最后。
    进程重启、客户端断开、BFF 超时都会让已完成的 LLM 工作全部丢失，前端能看到的
    只有一个纯客户端秒表。job 让执行与请求解耦，进度落盘，客户端断开不影响执行。
    """

    id: str
    project_id: str
    revision_id: str
    revision_etag: int = Field(ge=1)
    status: ModelingJobStatus = ModelingJobStatus.QUEUED
    stage: ModelingJobStage = ModelingJobStage.QUEUED
    progress: ModelingJobProgress = Field(default_factory=ModelingJobProgress)
    proposal_id: str | None = None
    error: str | None = Field(default=None, max_length=2_000)
    created_by: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            ModelingJobStatus.COMPLETED,
            ModelingJobStatus.FAILED,
            ModelingJobStatus.CANCELLED,
        }
