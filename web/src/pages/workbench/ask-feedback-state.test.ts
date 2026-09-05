import { describe, expect, it } from 'vitest';

import type { AnalyticsQueryFailure } from '../../api/types';
import {
  feedbackEmptyCopy,
  failureReason,
  feedbackFixTarget,
  feedbackRows,
} from './ask-feedback-state';

const record = (
  question: string,
  code: string,
  extra: Record<string, unknown> = {},
) =>
  ({
    question,
    code,
    message: '',
    phrase: '',
    resolution: '',
    count: 1,
    last_seen: '2026-09-04T00:00:00Z',
    ...extra,
  }) as any;

describe('系统没直接听懂的说法', () => {
  it('四种收场都翻译成建模者看得懂的话', () => {
    // 顺序由 SQL 的 ORDER BY 决定，这里只管翻译。
    const rows = feedbackRows([
      record('各城市有哪些门店', 'QUERY_EXECUTION_FAILED'),
      record('哪些门店售卖摩卡', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: '「摩卡」不在「商品名称」的已发布取值里',
      }),
    ]);

    expect(rows.map((row) => row.kind)).toEqual(['refused', 'unknown_value']);
    expect(rows[1].what).toContain('摩卡');
  });

  it('猜和反问这两种收场不再重复说明：标签加落点已经把话说完了', () => {
    // 「模型自己猜的 → 销售金额」已经完整；再写一句"没有匹配到这个说法，模型
    // 自己选了「销售金额」"是把同一件事说第二遍、成员名重复一次，那一整行文字
    // 会把说法和落点挤到第三行去。
    const [guessed] = feedbackRows([
      record('各门店的业绩', 'SEMANTIC_INFERRED', {
        kind: 'inferred',
        resolution: '销售金额',
      }),
    ]);
    const [asked] = feedbackRows([
      record('各门店的业绩', 'SEMANTIC_CLARIFIED', {
        kind: 'clarified',
        resolution: '销售金额',
      }),
    ]);

    expect(guessed.what).toBe('');
    expect(asked.what).toBe('');
    // 成员名没丢，它在 resolution 上——界面渲染的就是这个字段。
    expect(guessed.resolution).toBe('销售金额');
    expect(asked.resolution).toBe('销售金额');
    expect(guessed.fixableByAlias).toBe(true);
    expect(asked.fixableByAlias).toBe(true);
  });

  it('说明里从不出现内部状态名', () => {
    const rows = feedbackRows([
      record('哪些门店售卖卡布奇洛', 'UNKNOWN_FILTER_VALUE', {
        kind: 'unknown_value',
        message: '「卡布奇洛」不在「商品名称」的已发布取值里',
      }),
      record('花为什么是红色的', 'NO_SEMANTIC_MAPPING', { kind: 'refused' }),
    ]);

    for (const row of rows) {
      expect(row.what).not.toContain(row.kind);
      expect(row.what).not.toContain(row.code);
    }
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

  it('次数原样透传，不再自己数', () => {
    // 聚合和排序都在 SQL 里做了：前端只看得到当前这一页，自己数出来的次数只是
    // "这一页里出现了几次"（实测 21 次的说法散在三页，各显示 2/10/9 次）。
    const [row] = feedbackRows([record('各门店的业绩', 'NO_SEMANTIC_MAPPING', { count: 21 })]);

    expect(row.count).toBe(21);
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
    ).toEqual({ kind: 'metric', id: 'metric:sales', name: '销售金额', phrase: '' });
  });

  it('模型猜的成员一样给落点——猜对了也要补，下次可能猜错', () => {
    expect(
      feedbackFixTarget({ kind: 'inferred', resolution: '所在城市' }, catalog),
    ).toEqual({ kind: 'dimension', id: 'dim:city', name: '所在城市', phrase: '' });
  });

  it('未发布取值的近似建议落到那个维度值上', () => {
    expect(
      feedbackFixTarget({ kind: 'unknown_value', resolution: '卡布奇诺' }, catalog),
    ).toEqual({ kind: 'dimensionValue', id: 'dv:1', name: '卡布奇诺', phrase: '' });
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

describe('预填说法', () => {
  const catalog = {
    metrics: [{ id: 'metric:sales', name: '销售金额' }],
    dimensions: [],
    dimensionValues: [],
  };

  it('模型报的配对能同时填上说法和成员', () => {
    const target = feedbackFixTarget(
      { kind: 'inferred', resolution: '销售金额', phrase: '业绩' },
      catalog,
    );

    expect(target).toEqual({
      kind: 'metric',
      id: 'metric:sales',
      name: '销售金额',
      phrase: '业绩',
    });
  });

  it('没有说法就留空，不拿整句问题顶上', () => {
    // 拿整句问题当术语名是造假。
    const target = feedbackFixTarget({ kind: 'inferred', resolution: '销售金额' }, catalog);

    expect(target?.phrase).toBe('');
  });
});

describe('feedbackEmptyCopy', () => {
  it('待办和归档各说各的，不共用一句话', () => {
    // 站在归档上说"待办清空了"，而待办里正堆着二十几条——那是一句关于用户
    // 自己数据的假话。
    expect(feedbackEmptyCopy('open').title).not.toBe(feedbackEmptyCopy('archived').title);
    expect(feedbackEmptyCopy('archived').title).toContain('归档');
  });
});

describe('列表 key', () => {
  it('同一句问话按不同说法分出的两行，key 必须不同', () => {
    // key 相同会让 React 复用错 DOM 节点——实测切到归档时，上一屏待办的行留在
    // 原地不动，× 和「恢复」两种动作同时出现在一个列表里。
    const rows = feedbackRows([
      record('各城市有多少门店', 'SEMANTIC_INFERRED', {
        kind: 'inferred',
        resolution: '门店数量',
      }),
      record('各城市有多少门店', 'SEMANTIC_INFERRED', {
        kind: 'inferred',
        resolution: '门店数量',
        phrase: '多少',
      }),
    ]);

    expect(new Set(rows.map((row) => row.key)).size).toBe(2);
  });
});

describe('点赞点踩进同一张待处理列表', () => {
  /**
   * 前四类是系统自己察觉到的异常；这两类只有用户知道——查询成功、六道治理关
   * 全绿、数字也出来了，答得对不对系统看不出来。所以它们必须在这张列表上出现，
   * 而且带着用户自己写的那句话。
   */
  const row = (overrides: Partial<AnalyticsQueryFailure> = {}) =>
    ({
      kind: 'disliked',
      phrase: '',
      resolution: '',
      question: '各门店销售额是多少',
      stage: 'FINISHED',
      code: 'USER_DISLIKED_ANSWER',
      message: '指标口径不对；应该扣掉退款',
      count: 1,
      last_seen: '2026-09-05T00:00:00Z',
      ...overrides,
    }) as AnalyticsQueryFailure;

  it('点踩显示用户填的原因和补充说明', () => {
    const [item] = feedbackRows([row()]);

    expect(item.kind).toBe('disliked');
    expect(item.what).toBe('指标口径不对；应该扣掉退款');
    // 没有可直接采纳的正解，不给「补进词典」的快捷入口。
    expect(item.fixableByAlias).toBe(false);
  });

  it('点赞也在列表里，但不是缺口', () => {
    const [item] = feedbackRows([
      row({ kind: 'liked', code: 'USER_LIKED_ANSWER', message: '' }),
    ]);

    expect(item.kind).toBe('liked');
    expect(item.what).toBe('');
    expect(item.fixableByAlias).toBe(false);
  });
});
