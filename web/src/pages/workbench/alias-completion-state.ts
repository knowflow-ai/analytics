/**
 * 发布页就地补全别名的纯逻辑：摘要、去掉一个别名、投影成接口要的审核记录。
 *
 * 发布门指名了哪几项没做过别名审核，这里只处理这几项的草稿，不碰整包 AI 补全。
 */

import type { AnalyticsSemanticAliasReview } from '@analytics/api/types';

export interface AliasCompletionDraft extends AnalyticsSemanticAliasReview {
  resource_name: string;
}

const KIND_LABELS: Record<AliasCompletionDraft['resource_type'], string> = {
  metric: '指标',
  dimension: '维度',
  dimension_value: '取值',
};

export function resourceKindLabel(kind: AliasCompletionDraft['resource_type']): string {
  return KIND_LABELS[kind];
}

export function completionSummary(drafts: ReadonlyArray<AliasCompletionDraft>): string {
  if (drafts.length === 0) return '没有需要补的资源。';
  return `为 ${drafts.length} 项生成了别名草稿，看一眼再采用。`;
}

export function dropAlias(
  drafts: ReadonlyArray<AliasCompletionDraft>,
  resourceId: string,
  alias: string,
): AliasCompletionDraft[] {
  return drafts.map((draft) =>
    draft.resource_id === resourceId
      ? { ...draft, aliases: draft.aliases.filter((item) => item !== alias) }
      : draft,
  );
}

export function toAliasReviews(
  drafts: ReadonlyArray<AliasCompletionDraft>,
): AnalyticsSemanticAliasReview[] {
  return drafts.map((draft) => ({
    resource_type: draft.resource_type,
    resource_id: draft.resource_id,
    aliases: [...draft.aliases],
    display_name: draft.display_name ?? null,
  }));
}
