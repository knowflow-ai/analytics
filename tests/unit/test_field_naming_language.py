"""字段业务名必须是中文，两条建模路径都要有这条约束。

上游 LLMSemanticModeller 的指令是 "Create a Chinese name for the field"。
生产默认走 staged 路径，其 NAMING_SYSTEM 从一开始就写着「为一张表的字段起
中文业务名」——这条一直是对齐的。single_call 备用路径此前缺字段级中文要求，
现已补齐；实测对当前模型不改变行为（补前补后都是中文名），属于对弱模型的保险。

历史教训（2026-08-24 英文目录事故）：一个英文表项目的目录业务名全是物理列名
（segment / net_amount），中文用户 0/54 打不中。成因不是 prompt——是那次运行
的三个网关调用全部瞬时失败，workflow 静默兜底成物理列名（存库 suggestion 的
「仅规则判定，模型复核未执行」标记是证据）。修复见 workflow 的降级可见性改动。
"""

from __future__ import annotations

import inspect

from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.prompts import NAMING_SYSTEM


def test_staged_path_requires_chinese_field_names() -> None:
    """生产默认路径：这条从上游移植时就没丢，钉住不被后来的改动带偏。"""

    assert "中文业务名" in NAMING_SYSTEM


def test_single_call_path_requires_chinese_field_names() -> None:
    """备用路径：与上游对齐的字段级约束。"""

    source = inspect.getsource(AiSemanticModeller._messages)
    assert "name 必须是中文业务名" in source
    assert "不要照抄英文列名" in source


def test_alias_generation_follows_the_element_name_language() -> None:
    """别名 prompt 要求「与名称同语言」——它是对的，不要加语言约束。

    英文别名是「名字是英文」的下游后果；往别名 prompt 加中文限制会把中文目录
    里的英文缩写别名（GMV、SKU）也误伤。
    """

    source = inspect.getsource(AiSemanticModeller)
    assert source.count("名称同语言的常用业务别名") == 2
