import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Database, Table } from 'lucide-react';
import { listColumnProfiles, listFactChecks, runFactCheck, versionOf } from '@analytics/api/analytics';
import type {
  AnalyticsColumnProfile,
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


/**
 * 建模时量过的列画像，按「表.列」索引。纯读缓存，和核对状态一样只取一次。
 */
export function useColumnProfiles(projectId: string, revisionId: string) {
  const query = useQuery({
    queryKey: ['column-profiles', projectId, revisionId],
    queryFn: () => listColumnProfiles(projectId, revisionId),
    staleTime: 5 * 60_000,
  });
  const index = new Map<string, AnalyticsColumnProfile>();
  for (const table of query.data?.profiles ?? []) {
    for (const column of table.columns) index.set(`${table.table}.${column.column}`, column);
  }
  return index;
}

/**
 * 样例行：判断一列是不是账号、一个数值是不是档位，看一眼原始行比读任何统计量都快。
 *
 * 取的是模型的**受治理来源**（含行级过滤），所以预览里出现的行，问数时也查得到。
 */
export function ModelRowsPreview({
  projectId,
  revision,
  modelId,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  modelId: string;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const entry = useFactChecks(projectId, revision.id).get(factCheckKey('rows', modelId));
  const payload = entry?.result?.payload as
    | { columns?: string[]; rows?: unknown[][]; truncated?: boolean }
    | undefined;

  const run = useMutation({
    mutationFn: () =>
      runFactCheck(projectId, revision.id, versionOf(revision), { kind: 'rows', subject_id: modelId }),
    onSuccess: ({ entry: fresh }) => {
      queryClient.setQueryData(
        factChecksKey(projectId, revision.id),
        (previous: { entries: AnalyticsFactCheckEntry[] } | undefined) => {
          const rest = (previous?.entries ?? []).filter(
            (item) => !(item.kind === 'rows' && item.subject_id === modelId),
          );
          return { entries: [...rest, fresh] };
        },
      );
      setOpen(true);
    },
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          icon={<Table className="size-3.5" />}
          loading={run.isPending}
          onClick={() => (payload && !open ? setOpen(true) : run.mutate())}
        >
          {payload && !open ? '查看样例行' : '取样例行'}
        </Button>
        {open && payload && (
          <button
            type="button"
            className="text-[11px] text-slate-400 hover:text-slate-600"
            onClick={() => setOpen(false)}
          >
            收起
          </button>
        )}
      </div>
      {open && payload?.columns && (
        <div className="overflow-x-auto rounded-md border border-slate-200">
          <table className="min-w-full text-[11px]">
            <thead className="bg-slate-50 text-slate-500">
              <tr>
                {payload.columns.map((column) => (
                  <th key={column} className="whitespace-nowrap px-2 py-1 text-left font-medium">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(payload.rows ?? []).map((row, index) => (
                <tr key={index} className="border-t border-slate-100">
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="whitespace-nowrap px-2 py-1 text-slate-700">
                      {cell === null || cell === undefined ? (
                        <span className="text-slate-300">NULL</span>
                      ) : (
                        String(cell)
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && payload?.truncated && (
        <div className="text-[11px] text-slate-400">只取了前几行，不是全部数据。</div>
      )}
    </div>
  );
}
