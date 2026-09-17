import { describe, expect, it } from 'vitest';
import type { AnalyticsFactCheckEntry } from '@analytics/api/types';
import {
  describeFactCheck,
  observedCardinalityFix,
  percent,
} from './fact-check';

const entry = (
  over: Partial<AnalyticsFactCheckEntry['result'] & object> & { kind?: AnalyticsFactCheckEntry['kind'] },
  expired = false,
): AnalyticsFactCheckEntry => {
  const kind = over.kind ?? 'grain';
  return {
    kind,
    subject_id: 'score_model_acct',
    subject_hash: 'sha256:key',
    expired,
    result: {
      kind,
      subject_id: 'score_model_acct',
      subject_hash: 'sha256:key',
      status: 'blocking',
      payload: {},
      computed_at: new Date('2026-09-16T09:00:00Z').toISOString(),
      ...over,
    },
  };
};

const NOW = new Date('2026-09-16T12:00:00Z');

describe('用数据核对的读法', () => {
  it('没量过就是没量过，不给一个看起来像结论的空位', () => {
    expect(describeFactCheck(undefined)).toBeUndefined();
    expect(
      describeFactCheck({
        kind: 'grain',
        subject_id: 'm',
        subject_hash: 'h',
        result: null,
        expired: false,
      }),
    ).toBeUndefined();
  });

  it('主标识不唯一：数字在前，怎么改在后', () => {
    const summary = describeFactCheck(
      entry({
        status: 'blocking',
        payload: {
          identifier_field_ids: ['score_model_acct.zhhao'],
          total_rows: 44224,
          null_rows: 0,
          uniqueness_rate: 411 / 44224,
          message: '换一个在数据里唯一的列做主标识。',
        },
      }),
      NOW,
    );

    expect(summary?.tone).toBe('red');
    // 0.9% 与 0% 差一个数量级，四舍五入会把它抹平成「0%」
    expect(summary?.headline).toBe('44224 行 · 唯一率 0.9% · 空值 0');
    expect(summary?.advice).toContain('换一个');
    expect(summary?.measuredAt).toBe('统计于 3 小时前');
  });

  it('通过时不再复述一遍「一切正常」', () => {
    const summary = describeFactCheck(
      entry({
        status: 'passed',
        payload: {
          identifier_field_ids: ['orders.id'],
          total_rows: 3,
          null_rows: 0,
          uniqueness_rate: 1,
          message: '主标识在当前数据中非空且唯一。',
        },
      }),
      NOW,
    );

    expect(summary?.tone).toBe('green');
    expect(summary?.headline).toBe('3 行 · 唯一率 100% · 空值 0');
    expect(summary?.advice).toBe('');
  });

  it('没配主标识时让提示自己说话，不编一行零', () => {
    const summary = describeFactCheck(
      entry({
        status: 'warning',
        payload: {
          identifier_field_ids: [],
          total_rows: 0,
          message: '模型未配置主标识，无法用数据证明事实粒度。',
        },
      }),
      NOW,
    );

    expect(summary?.headline).toBe('模型未配置主标识，无法用数据证明事实粒度。');
  });

  it('关系读的是命中率、实测基数与扇出', () => {
    const summary = describeFactCheck(
      entry({
        kind: 'relation',
        status: 'warning',
        payload: {
          left_join_coverage: 1,
          right_join_coverage: 0.12,
          declared_cardinality: 'many_to_one',
          observed_cardinality: 'one_to_many',
          left_fanout_factor: 3.2,
          right_fanout_factor: 1,
          message: '声明与实测不一致。',
        },
      }),
      NOW,
    );

    expect(summary?.headline).toBe('左 100% / 右 12% 命中 · 实测 一对多 · 扇出 ×3.2');
  });

  it('超过一天的统计仍然显示，但说清楚可能已经过时', () => {
    const summary = describeFactCheck(
      entry(
        {
          status: 'passed',
          payload: { identifier_field_ids: ['orders.id'], total_rows: 3, uniqueness_rate: 1 },
          computed_at: new Date('2026-09-13T12:00:00Z').toISOString(),
        },
        true,
      ),
      NOW,
    );

    expect(summary?.measuredAt).toBe('统计于 3 天前 · 可能已过时');
  });
});

describe('采用实测基数', () => {
  it('只在声明与实测真的不一致时给出', () => {
    const inconsistent = entry({
      kind: 'relation',
      payload: { declared_cardinality: 'many_to_one', observed_cardinality: 'one_to_many' },
    });
    expect(observedCardinalityFix(inconsistent)).toEqual({
      declared: 'many_to_one',
      observed: 'one_to_many',
    });

    const agreeing = entry({
      kind: 'relation',
      payload: { declared_cardinality: 'many_to_one', observed_cardinality: 'many_to_one' },
    });
    expect(observedCardinalityFix(agreeing)).toBeUndefined();
  });

  it('量不出基数时不给按钮：空关系两边都为零，猜不出方向', () => {
    const unknown = entry({
      kind: 'relation',
      payload: { declared_cardinality: 'many_to_one', observed_cardinality: null },
    });
    expect(observedCardinalityFix(unknown)).toBeUndefined();
  });

  it('粒度核对没有基数可采用', () => {
    expect(observedCardinalityFix(entry({ payload: { declared_cardinality: 'many_to_one' } }))).toBeUndefined();
  });
});

describe('比例的读法', () => {
  it('小于百分之一的比例保留一位，不抹成 0%', () => {
    expect(percent(411 / 44224)).toBe('0.9%');
    expect(percent(0.999)).toBe('99.9%');
    expect(percent(1)).toBe('100%');
    expect(percent(0)).toBe('0%');
  });
});
