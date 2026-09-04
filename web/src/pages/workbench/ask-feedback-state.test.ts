import { describe, expect, it } from 'vitest';
import {
  FEEDBACK_FILTERS,
  failureReason,
  feedbackFixTarget,
  feedbackRows,
  feedbackSummary,
  matchesFeedbackFilter,
} from './ask-feedback-state';

const record = (
  question: string,
  code: string,
  extra: Record<string, unknown> = {},
) =>
  ({
    question,
    effective_question: question,
    stage: 'CANDIDATE_DISCOVERY',
    code,
    message: '',
    release_id: 'rel-1',
    dataset_ids: [],
    ...extra,
  }) as any;

describe('系统没接住的说法', () => {
  it('用户澄清后答出来的排最前：它自带正解，照着补别名就行', () => {
    const rows = feedbackRows([
      record('各城市有哪些门店', 'QUERY_EXECUTION_FAILED'),
      record('哪些门店售卖摩卡', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: '「摩卡」不在「商品名称」的已发布取值里',
      }),
      record('各门店的营业额', 'SEMANTIC_INFERRED', {
        kind: 'inferred',
        resolution: '销售金额',
      }),
      record('各门店的业绩', 'SEMANTIC_CLARIFIED', {
        kind: 'clarified',
        resolution: '销售金额',
      }),
    ]);

    expect(rows.map((row) => row.kind)).toEqual([
      'clarified',
      'inferred',
      'unknown_value',
      'refused',
    ]);
  });

  it('模型自己猜的排在用户确认之后、其余之前：正解不可靠但次数就是信号', () => {
    const [row] = feedbackRows([
      record('各门店的业绩', 'SEMANTIC_INFERRED', {
        kind: 'inferred',
        resolution: '销售金额',
      }),
    ]);

    expect(row.what).toContain('模型自己选了');
    expect(row.what).toContain('销售金额');
    expect(row.fixableByAlias).toBe(true);
    expect(row.what).not.toContain('inferred');
  });

  it('说的是发生了什么，不是内部状态名', () => {
    const [row] = feedbackRows([
      record('各门店的业绩', 'SEMANTIC_CLARIFIED', {
        kind: 'clarified',
        resolution: '销售金额',
      }),
    ]);

    expect(row.what).toContain('销售金额');
    expect(row.what).not.toContain('clarified');
    expect(row.what).not.toContain('SEMANTIC_CLARIFIED');
  });

  it('近似取值建议带上，没有就不编', () => {
    const [near, none] = feedbackRows([
      record('哪些门店售卖卡布奇洛', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: '「卡布奇洛」不在「商品名称」的已发布取值里',
        resolution: '卡布奇诺',
      }),
      record('哪些门店售卖摩卡', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: '「摩卡」不在「商品名称」的已发布取值里',
      }),
    ]);

    expect(near.what).toContain('卡布奇诺');
    expect(none.what).not.toContain('最接近');
  });

  it('带正解的两类都标成可补别名——那是唯一能让下次直达的动作', () => {
    const rows = feedbackRows([
      record('各门店的业绩', 'SEMANTIC_CLARIFIED', {
        kind: 'clarified',
        resolution: '销售金额',
      }),
      record('哪些门店售卖摩卡', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: 'x',
      }),
    ]);

    expect(rows.every((row) => row.fixableByAlias)).toBe(true);
  });

  it('同一问句的同一种收场只占一行，按次数降序', () => {
    const rows = feedbackRows([
      record('甲', 'NO_SEMANTIC_MAPPING'),
      record('乙', 'NO_SEMANTIC_MAPPING'),
      record('乙', 'NO_SEMANTIC_MAPPING'),
    ]);

    expect(rows.map((row) => [row.question, row.count])).toEqual([
      ['乙', 2],
      ['甲', 1],
    ]);
  });

  it('同一问句不同收场分开计数——处理方式也不同', () => {
    const rows = feedbackRows([
      record('各门店的业绩', 'NO_SEMANTIC_MAPPING'),
      record('各门店的业绩', 'SEMANTIC_CLARIFIED', {
        kind: 'clarified',
        resolution: '销售金额',
      }),
    ]);

    expect(rows).toHaveLength(2);
  });

  it('空问句被丢弃', () => {
    expect(feedbackRows([record('   ', 'NO_SEMANTIC_MAPPING')])).toEqual([]);
  });

  it('矛盾过滤条件标成可补别名：它多半就是说法没被覆盖', () => {
    expect(failureReason('S2SQL_CONTRADICTORY_FILTER').fixableByAlias).toBe(true);
  });

  it('没收录的错误码退回到通用说法，不假装知道原因', () => {
    expect(failureReason('WHATEVER').fixableByAlias).toBe(false);
  });
});

const catalog = {
  metrics: [{ id: 'metric:sales', name: '销售金额' }],
  dimensions: [{ id: 'dim:city', name: '所在城市' }],
  dimensionValues: [{ id: 'dv:1', display_name: '卡布奇诺' }],
};

describe('这条证据能落到词典的哪个成员上', () => {
  it('用户自己选过的成员，直接给出落点', () => {
    // clarified 的 resolution 是用户在澄清卡上亲手选的，最可靠的一类。
    expect(
      feedbackFixTarget({ kind: 'clarified', resolution: '销售金额' }, catalog),
    ).toEqual({ kind: 'metric', id: 'metric:sales', name: '销售金额' });
  });

  it('模型猜的成员一样给落点——猜对了也要补，下次可能猜错', () => {
    expect(
      feedbackFixTarget({ kind: 'inferred', resolution: '所在城市' }, catalog),
    ).toEqual({ kind: 'dimension', id: 'dim:city', name: '所在城市' });
  });

  it('未发布取值的近似建议落到那个维度值上', () => {
    expect(
      feedbackFixTarget({ kind: 'unknown_value', resolution: '卡布奇诺' }, catalog),
    ).toEqual({ kind: 'dimensionValue', id: 'dv:1', name: '卡布奇诺' });
  });

  it('正解在当前目录里找不到时不猜', () => {
    /**
     * 线上那一版的成员名可能已经被改掉了。找不到就让这条留在「要先诊断」里，
     * 而不是挑一个名字最像的——猜错比不猜更糟。
     */
    expect(
      feedbackFixTarget({ kind: 'clarified', resolution: '毛利率' }, catalog),
    ).toBeNull();
  });

  it('正解同时命中多个成员时不猜', () => {
    const ambiguous = {
      metrics: [{ id: 'metric:a', name: '金额' }],
      dimensions: [{ id: 'dim:b', name: '金额' }],
      dimensionValues: [],
    };

    expect(feedbackFixTarget({ kind: 'clarified', resolution: '金额' }, ambiguous)).toBeNull();
  });

  it('没答上来的那类没有正解，也就没有落点', () => {
    expect(feedbackFixTarget({ kind: 'refused', resolution: '' }, catalog)).toBeNull();
  });

  it('没有近似建议的未发布取值不给落点', () => {
    // 「深圳」不在已发布取值里、也没有相近取值时，硬塞给某个维度是造假。
    expect(feedbackFixTarget({ kind: 'unknown_value', resolution: '' }, catalog)).toBeNull();
  });
});

describe('页头的三个数', () => {
  const rows = feedbackRows([
    record('各门店的业绩', 'NO_SEMANTIC_MAPPING', { kind: 'clarified', resolution: '销售金额' }),
    record('各门店的业绩', 'NO_SEMANTIC_MAPPING', { kind: 'clarified', resolution: '销售金额' }),
    record('各供应商的销售额', 'DIMENSION_NOT_REACHABLE', { kind: 'refused' }),
  ]);

  it('说法按归并后的条数算，次数单独说', () => {
    // 同一句话问了两遍是一个词汇缺口，不是两个；但次数本身就是优先级。
    const summary = feedbackSummary(rows, catalog);

    expect(summary.sayings).toBe(2);
    expect(summary.occurrences).toBe(3);
  });

  it('「能补进词典」的口径和按钮完全一致', () => {
    /**
     * 早先页头写「补别名就能解决 19 条」，而挂着按钮的其实是 17 条——标签和按钮
     * 各算各的。现在两边都问同一个函数。
     */
    const summary = feedbackSummary(rows, catalog);
    const withButton = rows.filter((row) => feedbackFixTarget(row, catalog) !== null);

    expect(summary.fixable).toBe(withButton.length);
  });
});

describe('按接下来做什么分组', () => {
  it('有落点的进「能直接补进词典」', () => {
    const fix = { kind: 'metric' as const, id: 'metric:sales', name: '销售金额' };

    expect(matchesFeedbackFilter('dictionary', fix)).toBe(true);
    expect(matchesFeedbackFilter('diagnose', fix)).toBe(false);
  });

  it('没落点的进「要先诊断」', () => {
    expect(matchesFeedbackFilter('diagnose', null)).toBe(true);
    expect(matchesFeedbackFilter('dictionary', null)).toBe(false);
  });

  it('不按内部收场类型分组', () => {
    /**
     * 早先分成 clarified / inferred / unknown_value / refused 四类，于是
     * 「说了不认识的取值」被排在「补别名就能解决」之外，可它每条都挂着补进词典的
     * 按钮——标签和按钮说的不是一件事。
     */
    expect(FEEDBACK_FILTERS.map((item) => item.key)).toEqual(['all', 'dictionary', 'diagnose']);
  });
});
