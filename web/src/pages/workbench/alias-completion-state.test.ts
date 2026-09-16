import { describe, expect, it } from 'vitest';
import {
  completionSummary,
  dropAlias,
  resourceKindLabel,
  toAliasReviews,
  type AliasCompletionDraft,
} from './alias-completion-state';

const drafts: AliasCompletionDraft[] = [
  {
    resource_type: 'metric',
    resource_id: 'metric:default_count:model_orders',
    resource_name: '订单数量',
    aliases: ['单量', '订单数'],
    display_name: null,
  },
  {
    resource_type: 'dimension',
    resource_id: 'dimension_channel',
    resource_name: '渠道',
    aliases: ['渠道来源'],
    display_name: null,
  },
  {
    resource_type: 'dimension_value',
    resource_id: 'value_channel_app',
    resource_name: 'APP',
    aliases: ['手机端'],
    display_name: '应用内',
  },
];

describe('发布页就地补全别名', () => {
  it('摘要按资源数说话，用户不用数', () => {
    expect(completionSummary(drafts)).toBe('为 3 项生成了别名草稿，看一眼再采用。');
    expect(completionSummary([])).toBe('没有需要补的资源。');
  });

  it('资源类型用业务话说', () => {
    expect(resourceKindLabel('metric')).toBe('指标');
    expect(resourceKindLabel('dimension')).toBe('维度');
    expect(resourceKindLabel('dimension_value')).toBe('取值');
  });

  it('去掉一个别名不改动其它草稿，也不动原数组', () => {
    const next = dropAlias(drafts, 'metric:default_count:model_orders', '单量');
    expect(next[0].aliases).toEqual(['订单数']);
    expect(next[1]).toBe(drafts[1]);
    expect(drafts[0].aliases).toEqual(['单量', '订单数']);
  });

  it('采用时把草稿投影成接口要的审核记录，取值保留展示名', () => {
    expect(toAliasReviews(drafts)).toEqual([
      {
        resource_type: 'metric',
        resource_id: 'metric:default_count:model_orders',
        aliases: ['单量', '订单数'],
        display_name: null,
      },
      {
        resource_type: 'dimension',
        resource_id: 'dimension_channel',
        aliases: ['渠道来源'],
        display_name: null,
      },
      {
        resource_type: 'dimension_value',
        resource_id: 'value_channel_app',
        aliases: ['手机端'],
        display_name: '应用内',
      },
    ]);
  });
});
