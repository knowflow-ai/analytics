import { describe, expect, it } from 'vitest';
import {
  type MetricDefinitionSources,
  buildDefineParams,
  checkDefinition,
} from './metric-definition';

const sources: MetricDefinitionSources = {
  measures: [
    { name: '净额', agg: 'SUM', expr: 'net_amount', bizName: 'net_amount', isCreateMetric: 1 },
    { name: '退款', agg: 'SUM', expr: 'refund_amount', bizName: 'refund_amount', isCreateMetric: 1 },
  ],
  fieldColumns: ['net_amount', 'refund_amount', 'order_id'],
  metrics: [
    { id: 'metric:gmv', bizName: 'gmv', name: '成交额' },
    { id: 'metric:cnt', bizName: 'order_cnt', name: '订单数' },
  ],
};

describe('口径校验', () => {
  it('度量表达式禁止聚合函数', () => {
    /** 服务端 validate_measure_metric_expression:度量已自带 agg。 */
    const result = checkDefinition('MEASURE', 'SUM(net_amount)', sources);
    expect(result.error).toContain('自带聚合');
  });

  it('字段表达式必须带聚合函数', () => {
    expect(checkDefinition('FIELD', 'net_amount', sources).error).toContain('聚合函数');
    expect(checkDefinition('FIELD', 'SUM(net_amount)', sources).error).toBeNull();
  });

  it('引用未受治理的名字时列出可用来源', () => {
    const result = checkDefinition('MEASURE', 'gross_amount', sources);
    expect(result.error).toContain('gross_amount');
    expect(result.error).toContain('net_amount');
  });

  it('大小写不敏感并规范化成目录里的写法', () => {
    const result = checkDefinition('MEASURE', 'NET_AMOUNT - Refund_Amount', sources);
    expect(result.error).toBeNull();
    expect(result.resolved).toEqual(['net_amount', 'refund_amount']);
  });

  it('空表达式和无引用表达式都拦下', () => {
    expect(checkDefinition('MEASURE', '   ', sources).error).toBe('表达式不能为空');
    expect(checkDefinition('FIELD', 'COUNT(1)', sources).error).toContain('至少要引用一个来源');
  });

  it('拒绝表名限定', () => {
    expect(checkDefinition('MEASURE', 'orders.net_amount', sources).error).toContain('表名');
  });

  it('指标组合按其它指标的英文名解析', () => {
    const result = checkDefinition('METRIC', 'gmv / order_cnt', sources);
    expect(result.error).toBeNull();
    expect(result.resolved).toEqual(['gmv', 'order_cnt']);
  });
});

describe('params 拼装', () => {
  it('度量型带回目录里的完整度量对象', () => {
    const params = buildDefineParams('MEASURE', ' net_amount ', ' ', ['net_amount'], sources);
    expect(params.metricDefineByMeasureParams).toEqual({
      expr: 'net_amount',
      filterSql: null,
      measures: [sources.measures[0]],
    });
    expect(params.metricDefineByFieldParams).toBeNull();
    expect(params.metricDefineByMetricParams).toBeNull();
  });

  it('指标型只写 id 和英文名', () => {
    const params = buildDefineParams('METRIC', 'gmv / order_cnt', '', ['gmv', 'order_cnt'], sources);
    expect(params.metricDefineByMetricParams?.metrics).toEqual([
      { id: 'metric:gmv', bizName: 'gmv' },
      { id: 'metric:cnt', bizName: 'order_cnt' },
    ]);
  });

  it('保留固定过滤原文', () => {
    const params = buildDefineParams('FIELD', 'SUM(net_amount)', " status = 'paid' ", ['net_amount'], sources);
    expect(params.metricDefineByFieldParams?.filterSql).toBe("status = 'paid'");
  });

  it('支持中文度量与列名', () => {
    /** 线上目录里的名字基本都是中文,例如 COUNT(词条id)、遇难人数 - 生还人数。 */
    const cn: MetricDefinitionSources = {
      measures: [
        { name: '遇难人数', agg: 'SUM', expr: '遇难人数', bizName: '遇难人数', isCreateMetric: 1 },
        { name: '生还人数', agg: 'SUM', expr: '生还人数', bizName: '生还人数', isCreateMetric: 1 },
      ],
      fieldColumns: ['词条id', '遇难人数'],
      metrics: [],
    };
    expect(checkDefinition('MEASURE', '遇难人数 - 生还人数', cn).resolved).toEqual([
      '遇难人数',
      '生还人数',
    ]);
    expect(checkDefinition('FIELD', 'COUNT(词条id)', cn)).toEqual({
      error: null,
      resolved: ['词条id'],
    });
  });
});
