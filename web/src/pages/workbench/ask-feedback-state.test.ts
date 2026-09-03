import { describe, expect, it } from 'vitest';
import { failureReason, feedbackRows } from './ask-feedback-state';

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
