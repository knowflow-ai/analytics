"""列级权限合同：不可见成员在**检索之前**就被移出候选。

为什么必须在检索前：不可见成员的名字如果先进了证据，就会顺着既有链路出现在
澄清卡、下钻候选、自动理解 chip 和最终 LLM 的最小 Schema 里。名称泄漏比数据
泄漏隐蔽——用户拿不到「高管薪酬」的数字，但看得见这个指标存在、属于哪个实体、
业务定义是什么。所以过滤点只能在 `SemanticMapper.collect_evidence` 的检索入口
（关键词 / 术语展开 / MANIFEST 兜底 / Embedding 四条通道共用的那一份 entries），
不能放在结果或投影层。

索引本身始终是全量的一份：绝不按用户裁剪索引，只在消费侧收窄。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticElementType


class _StubEmbeddingGateway:
    """与 `sales_index` 同一个确定性 gateway：模型 ID 必须与索引快照一致。"""

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="test-embedding-v1",
            dimension=8,
            vectors=tuple(self._vector(text) for text in texts),
        )

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        values = [0.0] * 8
        for index, character in enumerate(text):
            values[(ord(character) + index) % len(values)] += 1.0
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return tuple(value / norm for value in values)

_ALL_MEMBERS = frozenset(
    {
        "net_revenue",
        "refund_amount",
        "gross_after_refund",
        "order_count",
        "region",
        "channel",
        "order_date",
        "customer_segment",
        "product",
        "sales_amount_term",
    }
)


@pytest.fixture
def mapper() -> SemanticMapper:
    return SemanticMapper(embedding_gateway=_StubEmbeddingGateway())


def _collect(mapper: SemanticMapper, sales_index, question: str, allowed):
    return mapper.collect_evidence(
        question=question,
        dataset_ids=("sales_dataset",),
        index=sales_index,
        allowed_element_ids=allowed,
    )


def _element_ids(evidence) -> set[str]:
    return {item.element_id for item in evidence.matches}


def test_invisible_metric_never_reaches_evidence(mapper, sales_index):
    """点名问不可见指标：它一条证据都不该有，连 MANIFEST 兜底那条也不该有。"""

    visible = _ALL_MEMBERS - {"refund_amount"}
    evidence = _collect(mapper, sales_index, "各区域的退款金额", visible)

    assert "refund_amount" not in _element_ids(evidence)
    # 同一问题里的可见成员必须照常召回，过滤不是把整轮打死。
    assert "region" in _element_ids(evidence)


def test_invisible_metric_name_never_appears_in_any_channel(mapper, sales_index):
    """名字级断言：四条通道的 detected_text / phrase 都不得出现该成员的说法。"""

    visible = _ALL_MEMBERS - {"refund_amount"}
    evidence = _collect(mapper, sales_index, "退款金额和净收入分别是多少", visible)

    surfaced = {item.detected_text for item in evidence.matches}
    surfaced |= {item.phrase for item in evidence.matches}
    assert "退款金额" not in surfaced
    assert "净收入" in surfaced


def test_dimension_values_follow_their_dimension(mapper, sales_index):
    """维度不可见时，它的取值也不可检索——否则「华东」会暴露存在一个区域维度。"""

    visible = _ALL_MEMBERS - {"region"}
    evidence = _collect(mapper, sales_index, "华东的净收入", visible)

    ids = _element_ids(evidence)
    assert "region" not in ids
    assert "region_east" not in ids
    assert "net_revenue" in ids


def test_visible_dimension_keeps_its_values(mapper, sales_index):
    evidence = _collect(mapper, sales_index, "华东的净收入", _ALL_MEMBERS)

    assert "region_east" in _element_ids(evidence)


def test_none_means_unrestricted(mapper, sales_index):
    """没有列限制的用户走 `None`，不是「全量列表」——后者会在 Release 新增成员时静默收窄。"""

    unrestricted = _collect(mapper, sales_index, "各区域的退款金额", None)

    assert "refund_amount" in _element_ids(unrestricted)


def test_empty_whitelist_is_fail_closed(mapper, sales_index):
    """空集合是「一个成员都看不到」，绝不能被当成「没有限制」。"""

    evidence = _collect(mapper, sales_index, "各区域的退款金额", frozenset())

    member_ids = {
        item.element_id
        for item in evidence.matches
        if item.element_type is not SemanticElementType.DATASET
    }
    assert member_ids == set()


def test_dataset_scope_anchors_survive_the_member_whitelist(mapper, sales_index):
    """作用域名是 Scope 锚点，不是列：列限制不该让用户失去逐字点名作用域的能力。"""

    evidence = _collect(mapper, sales_index, "销售经营的净收入", frozenset({"net_revenue"}))

    assert "sales_dataset" in _element_ids(evidence)


def test_term_expansion_cannot_smuggle_an_invisible_metric(mapper, sales_index):
    """术语描述会被二次关键词展开：术语可见不代表它指向的成员可见。"""

    # 「销售额」是术语，描述是「净收入」。净收入不可见时，术语展开出的那一跳
    # 也必须落空，否则术语就成了绕过白名单的后门。
    visible = _ALL_MEMBERS - {"net_revenue"}
    evidence = _collect(mapper, sales_index, "销售额是多少", visible)

    assert "net_revenue" not in _element_ids(evidence)


def test_map_passes_the_whitelist_through(mapper, sales_index):
    """`map()` 是另一条入口，不能只有 `collect_evidence` 收窄。"""

    result = mapper.map(
        question="各区域的退款金额",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
        allowed_element_ids=_ALL_MEMBERS - {"refund_amount"},
    )

    assert "refund_amount" not in {item.element_id for item in result.matches}
