import { describe, expect, it } from 'vitest';
import type { MetricDefinitionSources } from './metric-definition';
import {
  columnExpr,
  deriveDefineType,
  parseConditions,
  readShape,
  serializeConditions,
} from './metric-authoring';

const sources: MetricDefinitionSources = {
  measures: [
    { name: '净额', agg: 'SUM', expr: 'net_amount', bizName: 'net_amount', isCreateMetric: 1 },
    { name: '数量', agg: 'SUM', expr: 'qty', bizName: 'qty', isCreateMetric: 1 },
  ],
  fieldColumns: ['net_amount', 'qty', 'order_id', 'status'],
  metrics: [{ id: 'metric:gmv', bizName: 'gmv', name: '成交额' }],
};

describe('形态 → 定义方式（用户不选，系统定）', () => {
  it('选中的列正好有同聚合的受治理度量时走 MEASURE，继承它的治理', () => {
    /** 度量上带着 unit / alias / 口径说明，这些是建模治理过的，不该被重新发明。 */
    expect(deriveDefineType('column', { column: 'net_amount', aggregation: 'SUM' }, sources)).toBe(
      'MEASURE',
    );
    expect(columnExpr({ column: 'net_amount', aggregation: 'SUM' }, sources)).toBe('net_amount');
  });

  it('聚合方式和度量不一致时走 FIELD，表达式带聚合函数', () => {
    /** 「平均客单价」要 AVG，而净额度量是 SUM——这不是同一个口径。 */
    expect(deriveDefineType('column', { column: 'net_amount', aggregation: 'AVG' }, sources)).toBe(
      'FIELD',
    );
    expect(columnExpr({ column: 'net_amount', aggregation: 'AVG' }, sources)).toBe(
      'AVG(net_amount)',
    );
  });

  it('没有受治理度量的列一律走 FIELD', () => {
    expect(deriveDefineType('column', { column: 'order_id', aggregation: 'COUNT_DISTINCT' }, sources)).toBe(
      'FIELD',
    );
    expect(columnExpr({ column: 'order_id', aggregation: 'COUNT_DISTINCT' }, sources)).toBe(
      'COUNT(DISTINCT order_id)',
    );
  });

  it('由其它指标计算 → METRIC', () => {
    expect(deriveDefineType('metric', null, sources)).toBe('METRIC');
  });
});

describe('读回用户面前的形态', () => {
  it('MEASURE 型读成「对一列做统计」，聚合取自度量', () => {
    expect(readShape('MEASURE', 'net_amount', sources)).toEqual({
      shape: 'column',
      column: 'net_amount',
      aggregation: 'SUM',
    });
  });

  it('FIELD 型从表达式反推列与聚合', () => {
    expect(readShape('FIELD', 'AVG(net_amount)', sources)).toEqual({
      shape: 'column',
      column: 'net_amount',
      aggregation: 'AVG',
    });
    expect(readShape('FIELD', 'COUNT(DISTINCT order_id)', sources)).toEqual({
      shape: 'column',
      column: 'order_id',
      aggregation: 'COUNT_DISTINCT',
    });
  });

  it('METRIC 型读成「由其它指标计算」', () => {
    expect(readShape('METRIC', 'gmv - 1', sources)?.shape).toBe('metric');
  });

  it('跨列表达式读不成单列统计，回落到表达式编辑', () => {
    /** SUM(a) - SUM(b) 没有唯一来源列，选择器表达不了它。 */
    expect(readShape('FIELD', 'SUM(net_amount) - SUM(qty)', sources)).toBeNull();
  });
});

describe('限定条件：选择器 ↔ filterSql', () => {
  it('单条等于', () => {
    expect(parseConditions("status = 'paid'")).toEqual([
      { column: 'status', operator: '=', value: 'paid' },
    ]);
  });

  it('多条 AND，双引号列名也认', () => {
    expect(parseConditions('"status" = \'paid\' AND "qty" > 10')).toEqual([
      { column: 'status', operator: '=', value: 'paid' },
      { column: 'qty', operator: '>', value: '10' },
    ]);
  });

  it('IN 列表', () => {
    expect(parseConditions("status IN ('paid', 'shipped')")).toEqual([
      { column: 'status', operator: 'IN', value: 'paid，shipped' },
    ]);
  });

  it('选择器表达不了的写法回落成 null，界面转只读原文而不是悄悄改写它', () => {
    expect(parseConditions("status = 'paid' OR qty > 1")).toBeNull();
    expect(parseConditions('lower(status) = 1')).toBeNull();
    expect(parseConditions('a = b')).toBeNull();
  });

  it('空与 null 都是「没有限定」', () => {
    expect(parseConditions(null)).toEqual([]);
    expect(parseConditions('   ')).toEqual([]);
  });

  it('序列化回可被编译器解析的形状，并往返一致', () => {
    const conditions = [
      { column: 'status', operator: '=' as const, value: 'paid' },
      { column: 'qty', operator: '>' as const, value: '10' },
    ];
    const sql = serializeConditions(conditions);
    expect(sql).toBe('"status" = \'paid\' AND "qty" > 10');
    expect(parseConditions(sql)).toEqual(conditions);
  });

  it('值里的单引号转义，不会拼出坏 SQL', () => {
    const sql = serializeConditions([{ column: 'name', operator: '=', value: "O'Neil" }]);
    expect(sql).toBe('"name" = \'O\'\'Neil\'');
    expect(parseConditions(sql)).toEqual([{ column: 'name', operator: '=', value: "O'Neil" }]);
  });

  it('没有条件时序列化成 null，而不是空字符串', () => {
    expect(serializeConditions([])).toBeNull();
  });
});
