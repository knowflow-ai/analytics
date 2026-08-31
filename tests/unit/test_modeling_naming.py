from knowflow_analytics.modeling.naming import deterministic_field_aliases


def test_deterministic_field_aliases_are_metadata_only_and_auditable() -> None:
    assert deterministic_field_aliases("累计交易额（万）") == ("累计交易额",)
    assert deterministic_field_aliases("覆盖国家和地区数量") == ()
    assert deterministic_field_aliases("院校名称") == ()
    assert deterministic_field_aliases("二手车数量（万）") == ("二手车数量",)
    assert deterministic_field_aliases("评分") == ()
