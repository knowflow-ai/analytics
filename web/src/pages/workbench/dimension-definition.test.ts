import { describe, expect, it } from 'vitest';
import type { AnalyticsCatalogDimension } from '@analytics/api/types';
import {
  applyDimensionEditorValues,
  checkDimensionExpression,
  dimensionEditorInitial,
  dimensionKind,
} from './dimension-definition';

const columns = ['city_id', 'city_name', 'created_at', '词条id', '成立时间（年）', '500强排名'];

function dimension(overrides: Partial<AnalyticsCatalogDimension> = {}): AnalyticsCatalogDimension {
  return {
    id: 'dim:city',
    name: '城市',
    bizName: 'city_name',
    description: '下单城市',
    sensitiveLevel: 0,
    modelId: 'model:orders',
    type: 'categorical',
    expr: 'city_name',
    semanticType: 'CATEGORY',
    alias: '城市名,所在城市',
    defaultValues: [],
    dimValueMaps: [],
    ext: { keepMe: true },
    ...overrides,
  } as AnalyticsCatalogDimension;
}

describe('维度类型判定', () => {
  it('与编译器同一套规则', () => {
    /** catalog_compiler._compile_dimension:semanticType=date 或 type 含 time 都算时间。 */
    expect(dimensionKind(dimension())).toBe('categorical');
    expect(dimensionKind(dimension({ semanticType: 'ID' }))).toBe('identifier');
    expect(dimensionKind(dimension({ semanticType: 'DATE' }))).toBe('time');
    expect(dimensionKind(dimension({ type: 'time', semanticType: 'CATEGORY' }))).toBe('time');
    expect(dimensionKind(dimension({ type: 'partition_time', semanticType: 'CATEGORY' }))).toBe(
      'time',
    );
  });
});

describe('维度表达式校验', () => {
  it('聚合函数属于指标', () => {
    expect(checkDimensionExpression('COUNT(city_id)', columns).error).toContain('聚合');
  });

  it('表名限定在真表达式里被识别', () => {
    expect(checkDimensionExpression("orders.city_name || ''", columns).error).toContain('表名');
  });

  it('裸的 orders.city_name 与服务端一致当作整列名', () => {
    /** _quote_identifier 的字符集里没有点号,服务端会把它整体加引号再查,
     *  报的是 unknown field;这里跟着报未知字段并列出可用字段。 */
    const result = checkDimensionExpression('orders.city_name', columns);
    expect(result.error).toContain('orders.city_name');
    expect(result.error).toContain('city_name');
  });

  it('未知字段报错并列出可用字段', () => {
    const result = checkDimensionExpression('province_name', columns);
    expect(result.error).toContain('province_name');
    expect(result.error).toContain('city_name');
  });

  it('接受表达式与中文列名', () => {
    expect(checkDimensionExpression("CONCAT(city_name, '市')", columns).error).toBeNull();
    expect(checkDimensionExpression('词条id', columns).error).toBeNull();
  });

  it('整列名不做词法拆分', () => {
    /** 服务端 _quote_identifier 只按 ASCII 运算符判断;全角括号和前导数字都是
     *  列名的一部分,拆开会让线上目录里的维度全部报错。 */
    expect(checkDimensionExpression('成立时间（年）', columns)).toEqual({
      error: null,
      resolved: ['成立时间（年）'],
    });
    expect(checkDimensionExpression('500强排名', columns).error).toBeNull();
    expect(checkDimensionExpression('"成立时间（年）"', columns).error).toBeNull();
  });

  it('空表达式拦下', () => {
    expect(checkDimensionExpression('  ', columns).error).toBe('表达式不能为空');
  });
});

describe('维度表单往返', () => {
  it('读出表单值', () => {
    const values = dimensionEditorInitial(
      dimension({
        type: 'time',
        semanticType: 'DATE',
        expr: 'created_at',
        defaultValues: ['近7天'],
        typeParams: { isPrimary: 'true', timeGranularity: 'month' },
      }),
    );
    expect(values.kind).toBe('time');
    expect(values.timeGranularity).toBe('month');
    expect(values.aliases).toBe('城市名，所在城市');
    expect(values.defaultValues).toBe('近7天');
  });

  it('保留未编辑的字段', () => {
    const existing = dimension();
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), name: '下单城市' },
      columns,
    );
    expect(saved.name).toBe('下单城市');
    expect(saved.bizName).toBe('city_name');
    expect(saved.ext).toEqual({ keepMe: true });
    expect(saved.dimValueMaps).toEqual([]);
  });

  it('类型没改动时保留原始 type 字符串', () => {
    /** partition_time 在独立维度上与 time 等价,改写它会让 DTO 与建模产出对不上。 */
    const existing = dimension({ type: 'partition_time', semanticType: 'DATE', expr: 'created_at' });
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), name: '创建时间' },
      columns,
    );
    expect(saved.type).toBe('partition_time');
    expect(saved.semanticType).toBe('DATE');
  });

  it('改类型时写入规范取值', () => {
    const existing = dimension();
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), kind: 'time', expr: 'created_at' },
      columns,
    );
    expect(saved.type).toBe('time');
    expect(saved.semanticType).toBe('DATE');
    expect(saved.typeParams).toEqual({ isPrimary: 'true', timeGranularity: 'day' });
  });

  it('改成非时间类型时保留原 typeParams', () => {
    const existing = dimension({
      type: 'time',
      semanticType: 'DATE',
      expr: 'created_at',
      typeParams: { isPrimary: 'false', timeGranularity: 'month' },
    });
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), kind: 'categorical', expr: 'city_name' },
      columns,
    );
    expect(saved.type).toBe('categorical');
    expect(saved.typeParams).toEqual({ isPrimary: 'false', timeGranularity: 'month' });
  });

  it('时间维度保留原有 isPrimary', () => {
    const existing = dimension({
      type: 'time',
      semanticType: 'DATE',
      expr: 'created_at',
      typeParams: { isPrimary: 'false', timeGranularity: 'day' },
    });
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), timeGranularity: 'week' },
      columns,
    );
    expect(saved.typeParams).toEqual({ isPrimary: 'false', timeGranularity: 'week' });
  });

  it('表达式非法时原样保留旧值', () => {
    const existing = dimension();
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), expr: 'COUNT(city_id)' },
      columns,
    );
    expect(saved.expr).toBe('city_name');
  });

  it('别名与默认取值按中文顿号切分', () => {
    const existing = dimension();
    const saved = applyDimensionEditorValues(
      existing,
      { ...dimensionEditorInitial(existing), aliases: ' 城市名 、所在城市', defaultValues: '北京，上海' },
      columns,
    );
    expect(saved.alias).toBe('城市名,所在城市');
    expect(saved.defaultValues).toEqual(['北京', '上海']);
  });
});
