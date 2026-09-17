import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Database } from 'lucide-react';
import { listFactChecks, runFactCheck, versionOf } from '@analytics/api/analytics';
import type {
  AnalyticsFactCheckEntry,
  AnalyticsFactCheckKind,
  AnalyticsRevision,
} from '@analytics/api/types';
import { Badge, Button, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import { describeFactCheck, factCheckKey, indexFactChecks } from './fact-check';

/**
 * 「用数据核对」：把发布前那份证据搬到它该在的位置。
 *
 * 客户现场那根被标成主标识的列，唯一率 0.9%，而这个数字在 AI 建模第一步就量出来
 * 过——只是没人看得到。人的建模成本有一大半花在替机器收拾它本来能自己查清的事实，
 * 这个按钮就是把那份事实还回来。
 *
 * 两条路分得很开：整页的状态是**纯读缓存**（一次请求，不碰业务库），真去量一遍
 * 必须是用户主动点的（打业务库、限流、一次一个对象）。
 */

const factChecksKey = (projectId: string, revisionId: string) =>
  ['fact-checks', projectId, revisionId] as const;

/** 当前草稿里每个对象的核对状态。所有调用方共用一次请求。 */
export function useFactChecks(projectId: string, revisionId: string) {
  const query = useQuery({
    queryKey: factChecksKey(projectId, revisionId),
    queryFn: () => listFactChecks(projectId, revisionId),
    // 缓存键是内容寻址的，草稿一改，服务端自然给出新键、查不到旧结果；
    // 所以这里不需要跟着每次保存重取。
    staleTime: 30_000,
  });
  return indexFactChecks(query.data?.entries ?? []);
}

export function FactCheckControl({
  projectId,
  revision,
  kind,
  subjectId,
  readOnly = false,
  action = '用数据核对',
  hint,
  children,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  kind: AnalyticsFactCheckKind;
  subjectId: string;
  readOnly?: boolean;
  action?: string;
  /** 还没量过时说一句为什么值得量。 */
  hint?: string;
  /** 结论旁边的额外动作，例如「采用实测基数」。 */
  children?: (entry: AnalyticsFactCheckEntry) => React.ReactNode;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  // 同一个 queryKey，react-query 会把同屏所有控件合成一次请求。
  const entry = useFactChecks(projectId, revision.id).get(factCheckKey(kind, subjectId));
  const summary = describeFactCheck(entry);

  const run = useMutation({
    mutationFn: () => runFactCheck(projectId, revision.id, versionOf(revision), { kind, subject_id: subjectId }),
    onSuccess: ({ entry: fresh }) => {
      queryClient.setQueryData(
        factChecksKey(projectId, revision.id),
        (previous: { entries: AnalyticsFactCheckEntry[] } | undefined) => {
          const rest = (previous?.entries ?? []).filter(
            (item) => !(item.kind === fresh.kind && item.subject_id === fresh.subject_id),
          );
          return { entries: [...rest, fresh] };
        },
      );
    },
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          icon={<Database className="size-3.5" />}
          loading={run.isPending}
          disabled={readOnly}
          onClick={() => run.mutate()}
        >
          {summary ? '重新核对' : action}
        </Button>
        {summary && (
          <>
            <Badge tone={summary.tone}>{summary.headline}</Badge>
            <span className="text-[11px] text-slate-400">{summary.measuredAt}</span>
          </>
        )}
        {!summary && hint && <span className="text-[11px] text-slate-400">{hint}</span>}
      </div>
      {summary?.advice && (
        <div className="text-[11px] text-slate-600">{summary.advice}</div>
      )}
      {entry && children?.(entry)}
    </div>
  );
}
