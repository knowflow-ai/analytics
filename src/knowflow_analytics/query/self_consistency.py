"""自洽投票：同一问题独立生成多次，取多数。

解码带随机性，同一问题多次生成可能给出不同 S2SQL，但正确答案往往是多条独立
推理最容易撞到同一处的那个，错误则各有各的错法。上游让每次推理使用不同的
few-shot 样例组合（``PromptHelper.getFewShotExemplars`` 每次重新洗牌），测的是
「换一组示例模型还答不答得一样」——比只抖 temperature 更能区分「照抄示例」与
「真从 schema 推出来」。

这对 LLM 形态漂移直接有效：装饰性 ORDER BY 时有时无、计数指标被写成 SUM、
漏聚合漏 GROUP BY，都是低概率偶发，多数票会把它们压下去。但它只降低概率、
不消除，替代不了确定性护栏，只能叠在上面。

⚠ 上游 ``OnePassSCSqlGenStrategy`` 的实现有缺陷，这里只复刻投票语义。N 次输出
先塞进 ``Map<String, Prompt> output2Prompt``（key 是 SQL 字符串，相同 SQL 互相
覆盖），再把 ``output2Prompt.keySet()`` 交给 ``selfConsistencyVote``；而投票函数
靠数重复次数工作，拿到去重后的集合时所有计数都是 1，最终等价于「从去重候选里
挑 HashMap 迭代顺序的第一条」。票数在 put 那一步就已经丢光。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence


def self_consistency_vote(outputs: Sequence[str]) -> tuple[str, dict[str, float]]:
    """对 N 条独立生成的输出做多数投票。

    返回胜出的输出与每条输出的得票率。计数必须发生在去重之前，否则投票退化成
    任选一条——这正是上游丢失的性质。

    平票按输出文本排序取第一条：重放同一批输出必须选出同一条，否则评测不可复现。
    """

    if not outputs:
        raise ValueError("self-consistency vote requires at least one output")
    counts = Counter(outputs)
    total = len(outputs)
    winner = min(counts, key=lambda item: (-counts[item], item))
    return winner, {item: count / total for item, count in counts.items()}
