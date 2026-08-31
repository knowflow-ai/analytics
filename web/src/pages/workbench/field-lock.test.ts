import { describe, expect, it } from 'vitest';

import {
  DeletionPreviewGuard,
  describeDeletionEffects,
  relationReferences,
  roleChangeBlockers,
  type DeletionEffect,
} from './field-lock';

describe('删除影响预览竞态门', () => {
  it('同一时刻只允许一个预览请求', () => {
    const guard = new DeletionPreviewGuard();

    expect(guard.begin()).toBe(1);
    expect(guard.begin()).toBeNull();
  });

  it('取消后忽略迟到响应，并只接受后发的新请求', () => {
    const guard = new DeletionPreviewGuard();
    const stale = guard.begin()!;

    guard.cancel();
    const current = guard.begin()!;

    expect(guard.settle(stale)).toBe(false);
    expect(guard.active).toBe(true);
    expect(guard.settle(current)).toBe(true);
    expect(guard.active).toBe(false);
  });
});

const field = (over: Record<string, unknown>) =>
  ({ id: 'f1', model_id: 'm1', column: 'net_amount', kind: 'measure', ...over }) as never;

const measureMetric = (over: Record<string, unknown>) =>
  ({
    id: 'met1',
    name: '净金额',
    modelId: 'm1',
    metricDefineType: 'MEASURE',
    metricDefineByMeasureParams: {
      expr: 'net_amount',
      measures: [{ bizName: 'net_amount', name: '净金额', agg: 'SUM', expr: 'net_amount', isCreateMetric: 1 }],
    },
    ...over,
  }) as never;

describe('角色改动的阻断判定(复刻编译器 MEASURE 引用规则)', () => {
  it('度量字段被 MEASURE 型指标口径引用 → 阻断,指名道姓', () => {
    const blockers = roleChangeBlockers(field({}), [measureMetric({})]);
    expect(blockers.map((m) => m.name)).toEqual(['净金额']);
  });

  it('编译器按 casefold 匹配,判定也必须', () => {
    const blockers = roleChangeBlockers(field({ column: 'Net_Amount' }), [measureMetric({})]);
    expect(blockers).toHaveLength(1);
  });

  it('FIELD 型指标按物理列解析,列还在就不断 → 不阻断', () => {
    const metric = measureMetric({ metricDefineType: 'FIELD', metricDefineByMeasureParams: null });
    expect(roleChangeBlockers(field({}), [metric])).toEqual([]);
  });

  it('别的模型的同名 measure 不算', () => {
    expect(roleChangeBlockers(field({}), [measureMetric({ modelId: 'm2' })])).toEqual([]);
  });

  it('非度量字段改角色不删任何 measure → 永不阻断', () => {
    expect(roleChangeBlockers(field({ kind: 'identifier' }), [measureMetric({})])).toEqual([]);
    expect(roleChangeBlockers(field({ kind: 'dimension' }), [measureMetric({})])).toEqual([]);
  });

  it('引用其它列的 MEASURE 指标不算', () => {
    const metric = measureMetric({
      metricDefineByMeasureParams: { expr: 'refund', measures: [{ bizName: 'refund' }] },
    });
    expect(roleChangeBlockers(field({}), [metric])).toEqual([]);
  });
});

describe('关系引用(提示,不阻断)', () => {
  const relations = [
    {
      id: 'r1',
      left_model_id: 'm1',
      right_model_id: 'm2',
      conditions: [{ left_field_id: 'f1', right_field_id: 'f9' }],
    },
  ] as never[];

  it('join 条件引用该字段 → 列出', () => {
    expect(relationReferences(field({}), relations)).toHaveLength(1);
    expect(relationReferences(field({ id: 'f9' }), relations)).toHaveLength(1);
  });

  it('未被引用 → 空', () => {
    expect(relationReferences(field({ id: 'f-none' }), relations)).toEqual([]);
  });
});

describe('删除影响清单的翻译', () => {
  const names = new Map([
    ['dataset:ds1', '订单分析'],
    ['metric:met2', '订单数量'],
    ['query_rule:rule1', '最近七天'],
  ]);
  const effects: DeletionEffect[] = [
    { action: 'delete', resource_kind: 'metric', resource_id: 'met1', reason: '用户请求删除' },
    { action: 'unlink', resource_kind: 'dataset', resource_id: 'ds1', reason: '成员被删除' },
    { action: 'delete', resource_kind: 'metric', resource_id: 'met2', reason: '所属模型或依赖指标被删除' },
    { action: 'delete', resource_kind: 'dimension_value', resource_id: 'dv1', reason: '维度被删除' },
    { action: 'delete', resource_kind: 'dimension_value', resource_id: 'dv2', reason: '维度被删除' },
    { action: 'delete', resource_kind: 'semantic_context', resource_id: 'ctx1', reason: '语义目标被删除' },
    { action: 'delete', resource_kind: 'semantic_context', resource_id: 'ctx2', reason: '语义目标被删除' },
    { action: 'delete', resource_kind: 'query_rule', resource_id: 'rule1', reason: '查询作用域已失效' },
  ];

  it('翻译成人话:跳过删除对象本身,维度值合并计数,id 解析成名字', () => {
    const lines = describeDeletionEffects(
      effects,
      { resource_kind: 'metric', resource_id: 'met1' },
      names,
    );
    expect(lines).toEqual([
      '从查询作用域「订单分析」解除成员引用',
      '连带删除指标「订单数量」(所属模型或依赖指标被删除)',
      '连带删除查询规则「最近七天」(查询作用域已失效)',
      '删除 2 条维度值别名',
      '删除 2 条语义上下文',
    ]);
  });

  it('没有连带影响时返回空(调用方据此免确认)', () => {
    expect(
      describeDeletionEffects(
        [{ action: 'delete', resource_kind: 'metric', resource_id: 'met1', reason: '用户请求删除' }],
        { resource_kind: 'metric', resource_id: 'met1' },
        names,
      ),
    ).toEqual([]);
  });

  it('名字解析不到时退回 id,不吞行', () => {
    const lines = describeDeletionEffects(
      [{ action: 'unlink', resource_kind: 'dataset', resource_id: 'ds-unknown', reason: 'x' }],
      { resource_kind: 'metric', resource_id: 'met1' },
      names,
    );
    expect(lines).toEqual(['从查询作用域「ds-unknown」解除成员引用']);
  });

  it('跨类型同 ID 只跳过目标本身，不隐藏级联资源', () => {
    const effects: DeletionEffect[] = [
      { action: 'delete', resource_kind: 'model', resource_id: 'shared', reason: '用户请求删除' },
      { action: 'delete', resource_kind: 'metric', resource_id: 'shared', reason: '所属模型被删除' },
    ];
    const typedNames = new Map([
      ['model:shared', '商家'],
      ['metric:shared', '商家数量'],
    ]);

    expect(
      describeDeletionEffects(
        effects,
        { resource_kind: 'model', resource_id: 'shared' },
        typedNames,
      ),
    ).toEqual(['连带删除指标「商家数量」(所属模型被删除)']);
  });
});
