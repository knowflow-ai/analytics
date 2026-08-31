import { describe, expect, it } from 'vitest';
import type { AnalyticsRevision } from '@analytics/api/types';
import { computeCompleteness } from './completeness';

function revision(spec: Partial<AnalyticsRevision['semantic_spec']>): AnalyticsRevision {
  return {
    semantic_spec: {
      models: [],
      fields: [],
      relations: [],
      dimensions: [],
      metrics: [],
      datasets: [],
      ...spec,
    },
  } as AnalyticsRevision;
}

const metric = (over: Record<string, unknown> = {}) =>
  ({
    id: 'm1',
    name: '净收入',
    model_id: 'orders',
    kind: 'atomic',
    aliases: [],
    description: '',
    ...over,
  }) as never;

const dim = (over: Record<string, unknown> = {}) =>
  ({
    id: 'd1',
    name: '城市',
    model_id: 'orders',
    semantic_type: 'categorical',
    aliases: [],
    description: '',
    ...over,
  }) as never;

describe('建模完成度', () => {
  it('统计描述与别名覆盖并列出缺失项的业务名', () => {
    const gauges = computeCompleteness(
      revision({
        metrics: [metric({ description: '已扣退款', aliases: ['营收'] }), metric({ id: 'm2', name: '订单数' })],
        dimensions: [dim()],
      }),
    );
    const desc = gauges.find((g) => g.key === 'descriptions')!;
    expect(desc.covered).toBe(1);
    expect(desc.total).toBe(3);
    expect(desc.missing).toEqual(['订单数', '城市']);
    const alias = gauges.find((g) => g.key === 'aliases')!;
    expect(alias.missing).toEqual(['订单数', '城市']);
  });

  it('时间轴只在模型有多条时间维度时计入分母', () => {
    /** 只有一条时间列没有歧义,不该逼用户声明——total=0 表示该项不适用。 */
    const single = computeCompleteness(
      revision({
        metrics: [metric()],
        dimensions: [dim({ semantic_type: 'time' })],
      }),
    );
    expect(single.find((g) => g.key === 'timeAxis')!.total).toBe(0);

    const dual = computeCompleteness(
      revision({
        metrics: [metric(), metric({ id: 'm2', name: '订单数', agg_time_dimension_id: 'd2' })],
        dimensions: [
          dim({ id: 'd1', semantic_type: 'time' }),
          dim({ id: 'd2', name: '支付时间', semantic_type: 'time' }),
        ],
      }),
    );
    const axis = dual.find((g) => g.key === 'timeAxis')!;
    expect(axis.total).toBe(2);
    expect(axis.covered).toBe(1);
    expect(axis.missing).toEqual(['净收入']);
  });

  it('派生指标不进时间轴分母', () => {
    /** 派生指标的时间轴由依赖的原子指标决定,合同也禁止它自己声明。 */
    const gauges = computeCompleteness(
      revision({
        metrics: [metric({ kind: 'derived', formula: '{a}/{b}' })],
        dimensions: [
          dim({ id: 'd1', semantic_type: 'time' }),
          dim({ id: 'd2', name: '支付时间', semantic_type: 'time' }),
        ],
      }),
    );
    expect(gauges.find((g) => g.key === 'timeAxis')!.total).toBe(0);
  });
});
