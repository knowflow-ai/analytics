import { describe, expect, it } from 'vitest';
import type { AnalyticsCatalogMetric } from '@analytics/api/types';
import type { MetricDefinitionSources } from './metric-definition';
import { applyMetricEditorValues, metricEditorInitial } from './metric-editor';

/** MEASURE 型表达式只引用度量名——聚合由度量自带,服务端禁止再写 SUM()。 */
const NET_AMOUNT = {
  name: '净额',
  agg: 'SUM',
  expr: 'net_amount',
  bizName: 'net_amount',
  isCreateMetric: 1,
} as const;

const sources: MetricDefinitionSources = {
  measures: [NET_AMOUNT, { ...NET_AMOUNT, name: '退款', bizName: 'refund_amount', expr: 'refund_amount' }],
  fieldColumns: ['net_amount', 'refund_amount', 'order_id'],
  metrics: [{ id: 'metric:orders', bizName: 'order_cnt', name: '订单数' }],
};

function metric(overrides: Partial<AnalyticsCatalogMetric> = {}): AnalyticsCatalogMetric {
  return {
    id: 'metric:net_revenue',
    name: '净收入',
    bizName: 'net_revenue',
    description: '已扣退款',
    sensitiveLevel: 1,
    modelId: 'model:orders',
    alias: '营收,GMV',
    classifications: ['财务'],
    isTag: 0,
    ext: { keepMe: true },
    metricDefineType: 'MEASURE',
    metricDefineByMeasureParams: {
      expr: 'net_amount',
      filterSql: "status = 'paid'",
      measures: [{ ...NET_AMOUNT }],
    },
    metricDefineByFieldParams: null,
    metricDefineByMetricParams: null,
    ...overrides,
  } as AnalyticsCatalogMetric;
}

describe('指标编辑器', () => {
  it('把 DTO 读成表单值', () => {
    const values = metricEditorInitial(
      metric({
        dataFormatType: 'percent',
        dataFormat: { needMultiply100: true, decimalPlaces: 1 },
      }),
    );
    expect(values.metricDefineType).toBe('MEASURE');
    expect(values.expr).toBe('net_amount');
    expect(values.filterSql).toBe("status = 'paid'");
    expect(values.aliases).toBe('营收，GMV');
    expect(values.sensitiveLevel).toBe(1);
    expect(values.dataFormatType).toBe('percent');
    expect(values.needMultiply100).toBe(true);
  });

  it('保存时保留未编辑的口径字段', () => {
    /** 表单只覆盖治理属性;定义参数、ext 这些没有对应控件的字段必须原样带回,
     *  否则一次改名就会把口径静默清空。 */
    const existing = metric();
    const saved = applyMetricEditorValues(existing, {
      ...metricEditorInitial(existing),
      name: '净收入(新)',
    }, sources);
    expect(saved.metricDefineByMeasureParams).toEqual(existing.metricDefineByMeasureParams);
    expect(saved.metricDefineType).toBe('MEASURE');
    expect(saved.metricDefineByFieldParams).toBeNull();
    expect(saved.ext).toEqual({ keepMe: true });
    expect(saved.bizName).toBe('net_revenue');
    expect(saved.name).toBe('净收入(新)');
  });

  it('别名与分类按中文顿号切分并规范化', () => {
    const existing = metric();
    const saved = applyMetricEditorValues(existing, {
      ...metricEditorInitial(existing),
      aliases: ' 营收 ，GMV、 销售额 ',
      classifications: '财务，核心',
    }, sources);
    expect(saved.alias).toBe('营收,GMV,销售额');
    expect(saved.classifications).toEqual(['财务', '核心']);
  });

  it('清空格式时连同格式参数一起置空', () => {
    const existing = metric({
      dataFormatType: 'percent',
      dataFormat: { needMultiply100: true, decimalPlaces: 1 },
    });
    const saved = applyMetricEditorValues(existing, {
      ...metricEditorInitial(existing),
      dataFormatType: '',
    }, sources);
    expect(saved.dataFormatType).toBeNull();
    expect(saved.dataFormat).toBeNull();
  });



  it('改写表达式时按引用重建来源度量', () => {
    const existing = metric();
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), expr: 'net_amount - refund_amount' },
      sources,
    );
    expect(saved.metricDefineByMeasureParams?.expr).toBe('net_amount - refund_amount');
    expect(saved.metricDefineByMeasureParams?.measures.map((m) => m.bizName)).toEqual([
      'net_amount',
      'refund_amount',
    ]);
  });

  it('切换定义方式时只保留一个 params 对象', () => {
    /** 合同要求 metric must define exactly one params object。 */
    const existing = metric();
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), metricDefineType: 'FIELD', expr: 'SUM(net_amount)' },
      sources,
    );
    expect(saved.metricDefineType).toBe('FIELD');
    expect(saved.metricDefineByFieldParams).toEqual({
      expr: 'SUM(net_amount)',
      filterSql: "status = 'paid'",
      fields: [{ fieldName: 'net_amount' }],
    });
    expect(saved.metricDefineByMeasureParams).toBeNull();
    expect(saved.metricDefineByMetricParams).toBeNull();
  });

  it('固定过滤清空后写 null', () => {
    const existing = metric();
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), filterSql: '  ' },
      sources,
    );
    expect(saved.metricDefineByMeasureParams?.filterSql).toBeNull();
  });

  it('口径非法时原样保留旧定义', () => {
    const existing = metric();
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), expr: 'not_a_measure' },
      sources,
    );
    expect(saved.metricDefineByMeasureParams).toEqual(existing.metricDefineByMeasureParams);
  });

  it('读写聚合时间轴', () => {
    const existing = metric({ aggTimeDimensionId: 'dim:paid_at' });
    expect(metricEditorInitial(existing).aggTimeDimensionId).toBe('dim:paid_at');
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), aggTimeDimensionId: 'dim:ordered_at' },
      sources,
    );
    expect(saved.aggTimeDimensionId).toBe('dim:ordered_at');
  });

  it('清空聚合时间轴写 null 而不是空串', () => {
    /** 空串会被服务端当成一个不存在的维度 id 拒绝。 */
    const existing = metric({ aggTimeDimensionId: 'dim:paid_at' });
    const saved = applyMetricEditorValues(
      existing,
      { ...metricEditorInitial(existing), aggTimeDimensionId: '' },
      sources,
    );
    expect(saved.aggTimeDimensionId).toBeNull();
  });
});
