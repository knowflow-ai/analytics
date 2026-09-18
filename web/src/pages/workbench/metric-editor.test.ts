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

describe('新建指标', () => {
  it('从字段行进来时预填「对这一列求和」', () => {
    /** 位置即选择：点的是哪一行，就从那一列开始，不再多问一句「对一列还是由指标算」。 */
    const values = metricEditorInitial(null, { column: 'net_amount' });
    expect(values.metricDefineType).toBe('FIELD');
    expect(values.expr).toBe('SUM(net_amount)');
    expect(values.name).toBe('');
    expect(values.bizName).toBe('');
  });

  it('从复合指标分组进来时是空表达式', () => {
    const values = metricEditorInitial(null, { shape: 'metric' });
    expect(values.metricDefineType).toBe('METRIC');
    expect(values.expr).toBe('');
  });

  it('保存时写入英文标识——其它指标引用它时用的就是这个名字', () => {
    const base = {
      id: 'metric_abc',
      name: '',
      bizName: 'online_revenue',
      modelId: 'model:orders',
      metricDefineType: 'FIELD',
      metricDefineByFieldParams: null,
      metricDefineByMeasureParams: null,
      metricDefineByMetricParams: null,
    } as unknown as AnalyticsCatalogMetric;
    const saved = applyMetricEditorValues(
      base,
      {
        ...metricEditorInitial(null, { column: 'net_amount' }),
        bizName: 'online_revenue',
        name: '线上销售额',
      },
      sources,
    );
    expect(saved.bizName).toBe('online_revenue');
    expect(saved.name).toBe('线上销售额');
    expect(saved.metricDefineByFieldParams?.expr).toBe('SUM(net_amount)');
  });

  it('切到复合指标时清掉聚合时间轴', () => {
    /** 老坑：MEASURE 下选了时间轴再切 METRIC，保存必炸——contracts.MetricSpec
     *  只允许原子指标声明聚合时间轴，复合指标的时间轴由它引用的原子指标决定。 */
    const existing = metric({ aggTimeDimensionId: 'dimension:sale_date' } as never);
    const saved = applyMetricEditorValues(
      existing,
      {
        ...metricEditorInitial(existing),
        aggTimeDimensionId: 'dimension:sale_date',
        metricDefineType: 'METRIC',
        expr: 'order_cnt',
      },
      sources,
    );
    expect(saved.aggTimeDimensionId).toBeNull();
  });
});
