import { describe, expect, it } from 'vitest';
import {
  adoptableSuggestions,
  failureReason,
  groupFailures,
  withAlias,
} from './ask-feedback-state';

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

const suggestion = (
  detected: string,
  elementId: string | null,
  count = 1,
  kind = 'metric',
) =>
  ({
    id: `csug_${detected}`,
    detected_text: detected,
    selection_kind: kind,
    semantic_element_id: elementId,
    confirmation_count: count,
    latest_confirmed_at: '2026-09-01T00:00:00Z',
    status: 'pending_review',
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

describe('确认建议采纳', () => {
  const targets = [
    { kind: 'metric' as const, id: 'net_revenue', name: '净金额', aliases: ['净收入'] },
    { kind: 'dimension' as const, id: 'region', name: '地区', aliases: [] },
  ];

  it('把说法挂到用户当时确认的那个元素上', () => {
    const items = adoptableSuggestions(
      [suggestion('销售额', 'net_revenue', 3)],
      targets,
    );

    expect(items[0].target?.name).toBe('净金额');
    expect(items[0].alreadyCovered).toBe(false);
    expect(items[0].count).toBe(3);
  });

  it('目标已从目录里删掉时只读不可采纳，不静默挂到别的元素上', () => {
    const items = adoptableSuggestions(
      [suggestion('销售额', 'metric_that_is_gone')],
      targets,
    );

    expect(items[0].target).toBeNull();
  });

  it('已经是正名或别名的说法标为无需采纳', () => {
    const items = adoptableSuggestions(
      [suggestion('净收入', 'net_revenue'), suggestion('净金额', 'net_revenue')],
      targets,
    );

    expect(items.map((item) => item.alreadyCovered)).toEqual([true, true]);
  });

  it('维度值/业务对象确认不在此处采纳——它们不是别名问题', () => {
    const items = adoptableSuggestions(
      [suggestion('华东', 'region_east', 5, 'dimension_value')],
      targets,
    );

    expect(items).toEqual([]);
  });

  it('按确认次数降序：被反复确认的说法优先补', () => {
    const items = adoptableSuggestions(
      [
        suggestion('甲', 'net_revenue', 1),
        suggestion('乙', 'net_revenue', 9),
      ],
      targets,
    );

    expect(items.map((item) => item.detectedText)).toEqual(['乙', '甲']);
  });
});

describe('别名写入', () => {
  const target = {
    kind: 'metric' as const,
    id: 'net_revenue',
    name: '净金额',
    aliases: ['净收入'],
  };

  it('追加且保持已有顺序', () => {
    expect(withAlias(target, '销售额')).toEqual(['净收入', '销售额']);
  });

  it('重复、正名、空白都不写入', () => {
    expect(withAlias(target, '净收入')).toEqual(['净收入']);
    expect(withAlias(target, '净金额')).toEqual(['净收入']);
    expect(withAlias(target, '   ')).toEqual(['净收入']);
  });
});
