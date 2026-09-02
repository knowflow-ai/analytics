"""「这个项目该连哪个库」的接缝。

查询链路与建模链路都需要按项目取到执行器和方言，但它们不该认识数据源是怎么存的、
连接串怎么解密。所以这里只声明形状：``for_project(project_id)`` 给出一个带
``dialect`` 和 ``executor`` 的东西。

用协议而不是具体类型，是为了让上层不必导入目录层——那会把"查询"依赖到"存储"上，
而且测试里想塞一个假的都得先构造半套目录。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from knowflow_analytics.execution.dialect import SqlDialect

__all__ = ["ExecutionTarget", "ExecutionTargetProvider"]


@runtime_checkable
class ExecutionTarget(Protocol):
    """一个项目实际连的东西。"""

    dialect: SqlDialect
    executor: Any


@runtime_checkable
class ExecutionTargetProvider(Protocol):
    def for_project(self, project_id: str) -> ExecutionTarget: ...
