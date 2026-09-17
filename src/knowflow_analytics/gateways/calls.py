"""每次模型 / 向量调用的耗时记录。

一轮问数的时间几乎全在模型往返（实测非模型阶段合计 <1.5s，模型单次 7–60s），而此前
没有任何一处记它：阶段没有时间戳，网关不计时，RAGFlow 的访问日志只在终端里。57 秒
那一次到底是供应商慢、网关超时后重试、还是 prompt 太大，说不清——不记就只能盲调。

用 ContextVar 按请求收集：`capture_calls()` 在一轮问数开始时打开，网关每次调用
`record_call()`。没有打开时什么都不记（建模等其它路径零成本）。ThreadPoolExecutor
的工作线程不继承 ContextVar，自洽投票并行的那几票记不到——投票默认为 1，先接受。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CALLS: ContextVar[list[dict[str, Any]] | None] = ContextVar("analytics_calls", default=None)
_DEADLINE: ContextVar[float | None] = ContextVar("analytics_deadline", default=None)


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


@contextmanager
def question_budget(seconds: float | None) -> Iterator[None]:
    """一轮问数的墙钟预算。

    调用方（RAGFlow BFF）有自己的请求超时；重试链是「每次调用超时 × 尝试次数」，两者
    互不知情，于是**先响的是调用方**——用户看到一句没有诊断的「analytics request timed
    out」，而我们这边其实知道是哪一步卡住了。开了预算之后，模型调用的超时取「本档上限」
    与「还剩多少」的较小者，剩下的不够就直接不打这一发，把带诊断的超时还给用户。
    """

    token = _DEADLINE.set(None if seconds is None else time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_seconds() -> float | None:
    """这轮问数还剩多少秒。没开预算时返回 None。"""

    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()
