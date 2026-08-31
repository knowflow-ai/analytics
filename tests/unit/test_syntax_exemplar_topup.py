"""内置语法样例应当「补足」而不是「二选一」。

上游把 8 条系统样例(s2-exemplar.json)与用户 memory 样例放进同一个 embedding
collection,靠相似度竞争,PromptHelper 每次补足到 recall number 条——系统样例
始终在场。

我们此前是二选一:只要出现 1 条 reviewed exemplar,4 条语法样例全部消失。
第一条经人工确认的用例反而让 prompt 的方言示例整体断崖式减少。
"""

from __future__ import annotations

from knowflow_analytics.query.parser import select_prompt_syntax_exemplars
from knowflow_analytics.query.syntax_exemplars import SYNTAX_EXEMPLARS


def test_no_reviewed_exemplar_uses_all_builtin_samples() -> None:
    assert select_prompt_syntax_exemplars(reviewed_count=0) == list(SYNTAX_EXEMPLARS)


def test_one_reviewed_exemplar_does_not_wipe_out_the_builtins() -> None:
    """断崖是 bug 本身:1 条评审样例不该让 4 条语法样例全部消失。"""

    kept = select_prompt_syntax_exemplars(reviewed_count=1)
    assert kept, "第一条 reviewed exemplar 反而清空了语法样例"
    assert len(kept) == len(SYNTAX_EXEMPLARS) - 1


def test_enough_reviewed_exemplars_crowd_out_the_builtins() -> None:
    """评审样例是版本绑定的真实证据,足够多时不必再塞中性语法样例。"""

    assert select_prompt_syntax_exemplars(reviewed_count=len(SYNTAX_EXEMPLARS)) == []
    assert select_prompt_syntax_exemplars(reviewed_count=99) == []


def test_builtins_are_taken_in_declaration_order() -> None:
    """补足取前 N 条,顺序稳定,便于 prompt 快照比对。"""

    assert select_prompt_syntax_exemplars(reviewed_count=2) == list(SYNTAX_EXEMPLARS)[:2]
