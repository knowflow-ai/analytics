import { describe, expect, it } from "vitest";
import type { AnalyticsTerm } from "@analytics/api/types";
import {
  BUSINESS_DICTIONARY_SECTIONS,
  createDimensionValueDraft,
  createTermDraft,
  dimensionValueResourceFromDraft,
  termResourceFromDraft,
  validateTermDraft,
} from "./business-dictionary";

const existing: AnalyticsTerm = {
  id: "term-gmv",
  name: "成交额",
  description: "支付成功订单的成交金额。",
  aliases: ["GMV", "流水"],
  dataset_ids: ["legacy-scope"],
  metric_ids: ["metric-gmv"],
  dimension_ids: [],
};

describe("business dictionary contract", () => {
  it("groups governed terms and dimension values under one language surface", () => {
    expect(BUSINESS_DICTIONARY_SECTIONS).toEqual([
      { key: "terms", label: "业务术语" },
      { key: "dimensionValues", label: "维度值字典" },
    ]);
  });

  it("prefills contextual metric and dimension bindings when creating a term", () => {
    expect(
      createTermDraft(undefined, { metricId: "metric-gmv" }),
    ).toMatchObject({
      metricIds: ["metric-gmv"],
      dimensionIds: [],
    });
    expect(
      createTermDraft(undefined, { dimensionId: "dimension-region" }),
    ).toMatchObject({
      metricIds: [],
      dimensionIds: ["dimension-region"],
    });
  });

  it("从问数反馈跳过来时，用户那个说法要落进术语名", () => {
    // phrase 一度只走到 FeedbackFixTarget 就断了：跳转过来绑定填好了，术语名
    // 还是空的，用户得自己回去看刚才那条说的是哪个词。
    expect(
      createTermDraft(undefined, { metricId: "metric-gmv", name: "毛利如何" }),
    ).toMatchObject({ name: "毛利如何", metricIds: ["metric-gmv"] });
  });

  it("编辑已有术语时，它自己的名字优先于带过来的说法", () => {
    const term = {
      id: "term-1",
      name: "成交额",
      description: "",
      aliases: ["GMV"],
      metric_ids: ["metric-gmv"],
      dimension_ids: [],
      dataset_ids: [],
    } as Parameters<typeof createTermDraft>[0];

    expect(createTermDraft(term, { name: "毛利如何" })).toMatchObject({
      name: "成交额",
    });
  });

  it("没有说法的记录不填，行为和从前一样", () => {
    expect(
      createTermDraft(undefined, { metricId: "metric-gmv", name: "" }),
    ).toMatchObject({ name: "" });
  });

  it("requires a name and at least one governed metric or dimension binding", () => {
    expect(validateTermDraft(createTermDraft())).toBe("请输入术语名称");
    expect(validateTermDraft({ ...createTermDraft(), name: "成交额" })).toBe(
      "至少关联一个指标或维度",
    );
    expect(
      validateTermDraft({
        ...createTermDraft(),
        name: "成交额",
        metricIds: ["metric-gmv"],
      }),
    ).toBeNull();
  });

  it("normalizes aliases and preserves compatibility scope links during edits", () => {
    const draft = createTermDraft(existing);
    const resource = termResourceFromDraft(
      "term-gmv",
      {
        ...draft,
        aliasesText: " GMV，流水,GMV、交易额 ",
        dimensionIds: ["dimension-region"],
      },
      existing,
    );

    expect(resource).toEqual({
      id: "term-gmv",
      name: "成交额",
      description: "支付成功订单的成交金额。",
      aliases: ["GMV", "流水", "交易额"],
      dataset_ids: ["legacy-scope"],
      metric_ids: ["metric-gmv"],
      dimension_ids: ["dimension-region"],
    });
  });

  it("does not manufacture legacy query-scope links for a new term", () => {
    const resource = termResourceFromDraft("term-new", {
      ...createTermDraft(undefined, { metricId: "metric-gmv" }),
      name: "成交额",
    });
    expect(resource.dataset_ids).toEqual([]);
  });

  it("edits only governed dimension-value presentation fields", () => {
    const existingValue = {
      id: "value-east",
      dimension_id: "dimension-region",
      value: "E",
      display_name: "华东",
      aliases: ["东区"],
      enabled: true,
    };
    const draft = createDimensionValueDraft(existingValue);
    const resource = dimensionValueResourceFromDraft(existingValue, {
      ...draft,
      displayName: "华东地区",
      aliasesText: "东区，华东,东区",
      enabled: false,
    });

    expect(resource).toEqual({
      id: "value-east",
      dimension_id: "dimension-region",
      value: "E",
      display_name: "华东地区",
      aliases: ["东区", "华东"],
      enabled: false,
    });
  });
});
