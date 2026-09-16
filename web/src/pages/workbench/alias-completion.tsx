/**
 * 发布页就地补全别名。
 *
 * 发布门指名了哪几项还没做过别名审核。这里一个按钮只为这几项生成草稿，看一眼
 * 采用，审核记录随之补齐，然后重新校验。用户不用知道有「别名审核」这回事，
 * 也不用回建模页整包重跑。
 */

import { useMutation } from '@tanstack/react-query';
import { Sparkles, X } from 'lucide-react';
import { useState } from 'react';
import { applyAliasCompletion, suggestAliasCompletion } from '@analytics/api/analytics';
import type { AnalyticsRevision } from '@analytics/api/types';
import { Badge, Button, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import {
  completionSummary,
  dropAlias,
  resourceKindLabel,
  toAliasReviews,
  type AliasCompletionDraft,
} from './alias-completion-state';

export function AliasCompletionCard({
  projectId,
  revision,
  message,
  onApplied,
  onRevalidate,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  /** 发布门原话，已经是业务名。 */
  message: string;
  onApplied: (next: AnalyticsRevision) => void;
  onRevalidate: () => void;
}) {
  const toast = useToast();
  const [drafts, setDrafts] = useState<AliasCompletionDraft[] | null>(null);

  const suggest = useMutation({
    mutationFn: () => suggestAliasCompletion(projectId, revision.id, revision.etag),
    onSuccess: (completion) => setDrafts(completion.drafts),
    onError: (error) => toast.error(describeError(error)),
  });
  const apply = useMutation({
    mutationFn: () =>
      applyAliasCompletion(projectId, revision.id, revision.etag, toAliasReviews(drafts ?? [])),
    onSuccess: (next) => {
      setDrafts(null);
      onApplied(next);
    },
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2.5 text-xs text-red-700">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 leading-relaxed">{message}</div>
        {drafts === null && (
          <Button
            size="sm"
            variant="primary"
            icon={<Sparkles className="h-3.5 w-3.5" />}
            loading={suggest.isPending}
            onClick={() => suggest.mutate()}
          >
            补全缺失的别名
          </Button>
        )}
      </div>

      {drafts !== null && (
        <div className="mt-3 rounded-md border border-slate-200 bg-white px-3 py-2.5 text-slate-700">
          <div className="text-[11px] text-slate-500">{completionSummary(drafts)}</div>
          {drafts.length > 0 && (
            <ul className="mt-2 divide-y divide-slate-100">
              {drafts.map((draft) => (
                <li
                  key={`${draft.resource_type}:${draft.resource_id}`}
                  className="flex items-start gap-2 py-1.5"
                >
                  <Badge tone="slate">{resourceKindLabel(draft.resource_type)}</Badge>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold text-slate-800">
                      {draft.resource_name}
                      {draft.resource_type === 'dimension_value' && draft.display_name && (
                        <span className="ml-1 font-normal text-slate-500">
                          → {draft.display_name}
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {draft.aliases.length === 0 && (
                        <span className="text-[11px] text-slate-400">
                          没有生成别名，采用后只记录已审核
                        </span>
                      )}
                      {draft.aliases.map((alias) => (
                        <span
                          key={alias}
                          className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px]"
                        >
                          {alias}
                          <button
                            type="button"
                            aria-label={`去掉别名 ${alias}`}
                            className="text-slate-400 hover:text-red-600"
                            onClick={() => setDrafts(dropAlias(drafts, draft.resource_id, alias))}
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </span>
                      ))}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-2.5 flex items-center gap-2">
            {drafts.length > 0 ? (
              <Button
                size="sm"
                variant="primary"
                loading={apply.isPending}
                onClick={() => apply.mutate()}
              >
                采用并重新校验
              </Button>
            ) : (
              <Button size="sm" variant="primary" onClick={onRevalidate}>
                重新校验
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              loading={suggest.isPending}
              onClick={() => suggest.mutate()}
            >
              重新生成
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
