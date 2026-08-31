"""自洽投票:同一问题独立生成多次,取多数。

原理:解码带随机性,同一问题多次生成可能给出不同 SQL,但正确答案往往是多条
独立推理最容易撞到同一处的那个,错误则各有各的错法。上游让每次推理使用不同
的 few-shot 样例组合(getFewShotExemplars 每次重新洗牌),测的是「换一组示例
模型还答不答得一样」——比只抖 temperature 更能区分「照抄示例」与「真从
schema 推出来」。

⚠ 上游 OnePassSCSqlGenStrategy 的实现是坏的:N 次输出先塞进
`Map<String, Prompt> output2Prompt`(key 是 SQL 字符串,相同 SQL 互相覆盖),
再把 `output2Prompt.keySet()` 传给 selfConsistencyVote。而投票函数靠数重复
次数工作,拿到去重后的集合时所有计数都是 1,`count > maxCount` 只在第一条上
成立——等价于「从去重候选里挑 HashMap 迭代顺序的第一条」,票数在 put 那一步
就丢光了。跑 N 次花 N 倍 token,最后随机挑一条。

这里复刻的是投票语义,不是那个缺陷。
"""

from __future__ import annotations

import threading

import pytest

from knowflow_analytics.query.self_consistency import self_consistency_vote


def test_majority_wins() -> None:
    winner, shares = self_consistency_vote(["A", "B", "A"])
    assert winner == "A"
    assert shares["A"] == pytest.approx(2 / 3)
    assert shares["B"] == pytest.approx(1 / 3)


def test_duplicates_must_be_counted_not_deduplicated() -> None:
    """这是上游实现丢失的性质:去重后投票等于没投票。"""

    winner, shares = self_consistency_vote(["SUM(x)", "COUNT(x)", "COUNT(x)", "COUNT(x)"])
    assert winner == "COUNT(x)"
    assert shares["COUNT(x)"] == pytest.approx(0.75)


def test_single_output_wins_trivially() -> None:
    winner, shares = self_consistency_vote(["only"])
    assert winner == "only"
    assert shares == {"only": 1.0}


def test_ties_break_deterministically() -> None:
    """平票必须稳定:同一批输出每次跑都要选出同一条,否则重放不可复现。"""

    first = self_consistency_vote(["B", "A"])[0]
    second = self_consistency_vote(["A", "B"])[0]
    assert first == second


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        self_consistency_vote([])


class _ScriptedGateway:
    """按脚本依次返回 S2SQL,记录被调用次数。并行分发下取脚本必须原子。"""

    def __init__(self, sqls: list[str]) -> None:
        self._sqls = list(sqls)
        self.calls = 0
        self._lock = threading.Lock()

    def generate_json(self, *, purpose, messages, response_schema, trace):
        del purpose, messages, response_schema, trace
        with self._lock:
            sql = self._sqls[min(self.calls, len(self._sqls) - 1)]
            self.calls += 1
        return {"sql": sql, "thought": ""}


_GOOD = 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'
_DRIFT = 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域" ORDER BY SUM("净收入") DESC'


def _parse(gateway, sales_release, **kwargs):
    from knowflow_analytics.query.contracts import MapMode, MappingResult
    from knowflow_analytics.query.parser import LlmS2SqlParser

    return LlmS2SqlParser(gateway, **kwargs).parse(
        question="各区域净收入",
        release=sales_release,
        mapping=MappingResult(
            dataset_id="sales_dataset",
            mode=MapMode.STRICT,
            normalized_question="各区域净收入",
            matches=(),
            config_version="test",
        ),
        query_id="sc-test",
    )


def test_default_stays_single_inference(sales_release) -> None:
    """默认 1 次,与上游默认值一致,不给线上凭空加 N 倍模型开销。"""

    gateway = _ScriptedGateway([_GOOD])
    candidate = _parse(gateway, sales_release)
    assert gateway.calls == 1
    assert candidate.corrected_s2sql == _GOOD


def test_majority_output_wins_over_a_drifted_one(sales_release) -> None:
    """三次里两次一致:偶发漂移被票数压掉。"""

    gateway = _ScriptedGateway([_DRIFT, _GOOD, _GOOD])
    candidate = _parse(gateway, sales_release, self_consistency_number=3)
    assert gateway.calls == 3
    assert candidate.corrected_s2sql == _GOOD


def test_invalid_outputs_do_not_get_a_vote(sales_release) -> None:
    """语法不合法的输出直接出局,不参与投票——否则坏输出也能靠数量取胜。"""

    gateway = _ScriptedGateway(["这不是 SQL", _GOOD, "也不是 SQL"])
    candidate = _parse(gateway, sales_release, self_consistency_number=3)
    assert candidate.corrected_s2sql == _GOOD


class _BarrierGateway:
    """每票都等其他票到齐才返回:只有并行分发能让所有票同时抵达栅栏。"""

    def __init__(self, sql: str, parties: int) -> None:
        self._sql = sql
        self._barrier = threading.Barrier(parties, timeout=5.0)
        self._lock = threading.Lock()
        self.calls = 0

    def generate_json(self, *, purpose, messages, response_schema, trace):
        del purpose, messages, response_schema, trace
        with self._lock:
            self.calls += 1
        self._barrier.wait()
        return {"sql": self._sql, "thought": ""}


def test_ballots_are_dispatched_concurrently(sales_release) -> None:
    """三票必须同时在飞。串行实现第一票会独自把栅栏等破(BrokenBarrierError):
    自洽投票的三票互相独立,串行等待只是把单票延迟乘 N,一票挂死还会堵住全部。"""

    gateway = _BarrierGateway(_GOOD, parties=3)
    candidate = _parse(gateway, sales_release, self_consistency_number=3)
    assert gateway.calls == 3
    assert candidate.corrected_s2sql == _GOOD


class _GovernanceGateway:
    """第一票就抛治理级错误,其余票返回合法 SQL。"""

    def __init__(self, sql: str) -> None:
        self._sql = sql
        self._lock = threading.Lock()
        self.calls = 0

    def generate_json(self, *, purpose, messages, response_schema, trace):
        del purpose, messages, response_schema
        with self._lock:
            self.calls += 1
        if trace["attempt"] == "1":
            from knowflow_analytics.query.errors import SemanticParsingError

            raise SemanticParsingError(
                "值必须来自已发布字典", code="LLM_S2SQL_GROUNDED_VALUE_REQUIRED"
            )
        return {"sql": self._sql, "thought": ""}


def test_a_governance_blocking_ballot_still_aborts_the_parse(sales_release) -> None:
    """治理级错误(如值未接地)不因并行化被投票吞掉:哪怕其余票合法也要上抛。"""

    from knowflow_analytics.errors import AnalyticsError

    gateway = _GovernanceGateway(_GOOD)
    with pytest.raises(AnalyticsError) as exc_info:
        _parse(gateway, sales_release, self_consistency_number=3)
    assert exc_info.value.code == "LLM_S2SQL_GROUNDED_VALUE_REQUIRED"
