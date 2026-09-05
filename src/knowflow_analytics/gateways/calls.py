"""每次模型 / 向量调用的耗时记录。

一轮问数的时间几乎全在模型往返（实测非模型阶段合计 <1.5s，模型单次 7–60s），而此前
没有任何一处记它：阶段没有时间戳，网关不计时，RAGFlow 的访问日志只在终端里。57 秒
那一次到底是供应商慢、网关超时后重试、还是 prompt 太大，说不清——不记就只能盲调。

用 ContextVar 按请求收集：`capture_calls()` 在一轮问数开始时打开，网关每次调用
`record_call()`。没有打开时什么都不记（建模等其它路径零成本）。ThreadPoolExecutor
的工作线程不继承 ContextVar，自洽投票并行的那几票记不到——投票默认为 1，先接受。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CALLS: ContextVar[list[dict[str, Any]] | None] = ContextVar("analytics_calls", default=None)


@contextmanager
def capture_calls() -> Iterator[list[dict[str, Any]]]:
    """在这个块里发生的模型 / 向量调用都记到返回的列表里。"""

    calls: list[dict[str, Any]] = []
    token = _CALLS.set(calls)
    try:
        yield calls
    finally:
        _CALLS.reset(token)


def record_call(**fields: Any) -> None:
    calls = _CALLS.get()
    if calls is not None:
        calls.append(fields)
