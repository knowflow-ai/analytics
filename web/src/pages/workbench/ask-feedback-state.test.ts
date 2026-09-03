import { describe, expect, it } from 'vitest';
import { failureReason, groupFailures } from './ask-feedback-state';

const failure = (question: string, code: string) =>
  ({
    question,
    effective_question: question,
    stage: 'CANDIDATE_DISCOVERY',
    code,
    message: '',
    release_id: 'rel-1',
    spec_hash: 'sha256:spec',
    index_snapshot_id: 'idx-1',
    dataset_ids: [],
    details: {},
  }) as any;


describe('拒答问题聚合', () => {
  it('同一问句只占一行，按被拒次数排序', () => {
    const groups = groupFailures([
      failure('销售额是多少', 'NO_SEMANTIC_MAPPING'),
      failure('各地区毛利', 'NO_SEMANTIC_MAPPING'),
      failure('销售额是多少', 'NO_SEMANTIC_MAPPING'),
      failure('销售额是多少', 'NO_SEMANTIC_MAPPING'),
    ]);

    expect(groups.map((item) => [item.question, item.count])).toEqual([
      ['销售额是多少', 3],
      ['各地区毛利', 1],
    ]);
  });

  it('同一问句不同失败原因分开计数——原因不同，处理方式也不同', () => {
    const groups = groupFailures([
      failure('销售额是多少', 'NO_SEMANTIC_MAPPING'),
      failure('销售额是多少', 'AMBIGUOUS_QUERY_SCOPE'),
    ]);

    expect(groups).toHaveLength(2);
  });

  it('标出哪些拒答可能靠补别名解决', () => {
    expect(failureReason('NO_SEMANTIC_MAPPING').fixableByAlias).toBe(true);
    // 歧义、路径不可达、能力缺失都不是别名问题，别误导建模者去加别名。
    expect(failureReason('AMBIGUOUS_QUERY_SCOPE').fixableByAlias).toBe(false);
    expect(failureReason('DIMENSION_NOT_REACHABLE').fixableByAlias).toBe(false);
    expect(failureReason('UNSUPPORTED_ANALYTIC_OPERATION').fixableByAlias).toBe(
      false,
    );
    // 未知错误码不假装懂。
    expect(failureReason('SOMETHING_NEW').fixableByAlias).toBe(false);
  });

  it('空问句被丢弃', () => {
    expect(groupFailures([failure('   ', 'NO_SEMANTIC_MAPPING')])).toEqual([]);
  });
});
