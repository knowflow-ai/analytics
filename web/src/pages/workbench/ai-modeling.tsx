import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { Sparkles, Wand2 } from 'lucide-react';
import {
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';
import {
  applyProposal,
  cancelModelingJob,
  getModelingJob,
  getProposal,
  saveProposal,
  startModelingJob,
  versionOf,
} from '@analytics/api/analytics';
import type { AnalyticsModelingJob, AnalyticsModelingProposal, AnalyticsSuggestion } from '@analytics/api/types';
import { Badge, Button, Empty, Input, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import { allSemanticContextEntries, buildProposalScopeRows } from './catalog-view';
import type { WorkbenchContext } from './index';

export const modelingJobStorageKey = (revisionId: string) =>
  `knowflow-analytics.modeling-job.${revisionId}`;

export interface ModelingJobSession {
  jobId: string | null;
  setJobId: Dispatch<SetStateAction<string | null>>;
  job: UseQueryResult<AnalyticsModelingJob, Error>;
  proposal: UseQueryResult<AnalyticsModelingProposal, Error>;
}

export function isCurrentModelingJob(
  job: Pick<AnalyticsModelingJob, 'revision_etag'> | undefined,
  revisionEtag: number,
): boolean {
  return job?.revision_etag === revisionEtag;
}

export function hasReviewableModelingProposal(
  job: Pick<AnalyticsModelingJob, 'status' | 'revision_etag'> | undefined,
  proposal: Pick<AnalyticsModelingProposal, 'status'> | undefined,
  revisionEtag: number,
): boolean {
  return Boolean(
    job?.status === 'completed' &&
      isCurrentModelingJob(job, revisionEtag) &&
      proposal?.status === 'draft',
  );
}

export function useModelingJobSession(
  projectId: string,
  revisionId: string,
): ModelingJobSession {
  const [jobId, setJobId] = useState<string | null>(() =>
    typeof window === 'undefined'
      ? null
      : window.sessionStorage.getItem(modelingJobStorageKey(revisionId)),
  );
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const key = modelingJobStorageKey(revisionId);
    if (jobId) window.sessionStorage.setItem(key, jobId);
    else window.sessionStorage.removeItem(key);
  }, [jobId, revisionId]);

  const job = useQuery({
    queryKey: ['modeling-job', projectId, jobId],
    queryFn: () => getModelingJob(projectId, jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'queued' || status === 'running' ? 2000 : false;
    },
  });
  const proposalId = job.data?.status === 'completed' ? job.data.proposal_id : null;
  const proposal = useQuery({
    queryKey: ['proposal', projectId, revisionId, proposalId],
    queryFn: () => getProposal(projectId, revisionId, proposalId!),
    enabled: Boolean(proposalId),
  });
  useEffect(() => {
    if (proposal.data?.status === 'applied') setJobId(null);
  }, [proposal.data?.status, setJobId]);
  return { jobId, setJobId, job, proposal };
}

const FIELD_KIND_LABELS: Record<string, string> = {
  identifier: '标识',
  dimension: '维度',
  time: '时间',
  measure: '度量',
  field: '普通字段',
};
const AGG_LABELS: Record<string, string> = {
  sum: '求和',
  count: '计数',
  count_distinct: '去重计数',
  avg: '平均',
  min: '最小',
  max: '最大',
};

function describeChange(key: string, value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  switch (key) {
    case 'name':
      return `名称 → ${String(value)}`;
    case 'biz_name':
      return `英文标识 → ${String(value)}`;
    case 'description':
      return `说明：${String(value)}`;
    case 'kind':
      return `角色 → ${FIELD_KIND_LABELS[String(value)] ?? String(value)}`;
    case 'identifier_type':
      return value === 'primary' ? '主标识' : '外部标识';
    case 'default_aggregation':
      return `默认聚合 ${AGG_LABELS[String(value)] ?? String(value)}`;
    case 'unit':
      return `单位 ${String(value)}`;
    case 'create_metric':
      return value ? '生成指标' : null;
    case 'create_dimension':
      return value ? '生成维度' : null;
    case 'cardinality':
      return `基数 ${String(value)}`;
    case 'semantic_expr':
    case 'dimension_type':
    case 'aggregation':
      return key === 'aggregation' ? `聚合 ${AGG_LABELS[String(value)] ?? String(value)}` : null;
    default:
      return typeof value === 'object' ? null : `${key} → ${String(value)}`;
  }
}

export function AiModelingPanel({
  projectId,
  revision,
  acceptRevision,
  readOnly,
  goTo,
  modelingSession,
}: WorkbenchContext & { modelingSession: ModelingJobSession }) {
  const toast = useToast();
  const { jobId, setJobId, job, proposal } = modelingSession;

  const start = useMutation({
    mutationFn: () => startModelingJob(projectId, revision.id, revision.etag),
    onSuccess: (next) => setJobId(next.id),
    onError: (error) => toast.error(describeError(error)),
  });
  const cancel = useMutation({
    mutationFn: () => cancelModelingJob(projectId, jobId!),
    onSuccess: () => job.refetch(),
    onError: (error) => toast.error(describeError(error)),
  });

  // A proposal made for an older etag can no longer be applied; start over.
  const jobStale = Boolean(job.data && !isCurrentModelingJob(job.data, revision.etag));
  const stale = Boolean(proposal.data && proposal.data.revision_etag !== revision.etag);
  const applied = proposal.data?.status === 'applied';

  if (readOnly) {
    return <Empty title="当前版本只读" hint="派生一个新草稿后才能重新运行 AI 建模。" />;
  }

  // A job id that no longer resolves (catalog reset, another revision) must
  // not trap the user on a spinner: drop it and offer a fresh start.
  const lookupError = job.isError ? job.error : proposal.isError ? proposal.error : null;
  if (jobId && lookupError) {
    return (
      <Empty
        title="找不到上次的建模任务"
        hint={describeError(lookupError)}
        action={
          <Button variant="primary" onClick={() => setJobId(null)}>
            重新开始
          </Button>
        }
      />
    );
  }

  if (
    !jobId ||
    job.data?.status === 'cancelled' ||
    job.data?.status === 'failed' ||
    jobStale ||
    stale ||
    applied
  ) {
    return (
      <div className="mx-auto max-w-2xl px-6 py-12">
        <div className="flex flex-col items-center text-center">
          <span className="grid h-12 w-12 place-items-center rounded-2xl bg-gradient-to-br from-blue-600 to-blue-400 text-white shadow-md">
            <Sparkles className="h-6 w-6" />
          </span>
          <h2 className="mt-4 text-base font-semibold text-slate-900">AI 自动建模</h2>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            一次运行完成四件事：为每张表和字段起业务名称、判定字段角色（标识 / 维度 / 时间 / 度量）并生成指标，
            为指标、维度和常见维度值生成别名，补充可审核的语义上下文，再由完整目录确定性编译安全的查询作用域。
            目录结果只是草稿；查询作用域是只读预览。
          </p>
          {job.data?.status === 'failed' && (
            <div className="mt-4 w-full rounded-md border border-red-200 bg-red-50 px-3 py-2 text-left text-xs text-red-700">
              上次运行失败：{job.data.error ?? '未知错误'}
            </div>
          )}
          {(jobStale || stale) && (
            <div className="mt-4 text-xs text-amber-600">模型在生成建议后又被修改过，需要重新运行。</div>
          )}
          {applied && (
            <div className="mt-4 text-xs text-emerald-600">上次建议已采用。可以再次运行以获取新的建议。</div>
          )}
          <Button
            variant="primary"
            className="mt-6"
            icon={<Wand2 className="h-4 w-4" />}
            loading={start.isPending}
            disabled={revision.semantic_spec.models.length === 0}
            onClick={() => start.mutate()}
          >
            开始 AI 建模
          </Button>
          <div className="mt-3 text-[11px] text-slate-400">
            当前 {revision.semantic_spec.models.length} 个实体 · 约需 {Math.max(1, revision.semantic_spec.models.length)}–
            {Math.max(2, revision.semantic_spec.models.length * 2)} 分钟
          </div>
        </div>
      </div>
    );
  }

  if (!job.data || job.data.status === 'queued' || job.data.status === 'running') {
    return <JobProgress job={job.data} onCancel={() => cancel.mutate()} cancelling={cancel.isPending} />;
  }

  if (proposal.isPending || !proposal.data) return <Spinner label="正在加载建模建议…" />;

  return (
    <ProposalReview
      projectId={projectId}
      revisionId={revision.id}
      revisionVersion={versionOf(revision)}
      proposal={proposal.data}
      context={{ projectId, revision, acceptRevision, readOnly, goTo }}
      onApplied={() => setJobId(null)}
    />
  );
}

function JobProgress({
  job,
  onCancel,
  cancelling,
}: {
  job: AnalyticsModelingJob | undefined;
  onCancel: () => void;
  cancelling: boolean;
}) {
  const tables = job?.progress.tables ?? [];
  const done = tables.filter((t) => t.status === 'completed').length;
  const stage = job?.stage ?? 'queued';
  const stageLabel = { queued: '排队中', modeling: '理解表结构', enriching: '补全语义目录与查询作用域', done: '完成' }[stage];
  // Per-table modeling is only the first leg; catalog enrichment and scope compilation run
  // afterwards with no per-item progress, so it gets a fixed share of the bar.
  const percent =
    stage === 'done'
      ? 100
      : stage === 'enriching'
        ? 85
        : stage === 'modeling'
          ? 5 + (tables.length ? (done / tables.length) * 65 : 0)
          : 3;
  const hint =
    stage === 'enriching'
      ? '表结构已理解完毕，正在生成别名、语义上下文与只读查询作用域，通常需要 1–2 分钟。'
      : `${done}/${tables.length} 张表完成。可以离开页面，进度会保留。`;
  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-slate-900">AI 建模进行中 · {stageLabel}</div>
          <div className="mt-0.5 text-xs text-slate-400">{hint}</div>
        </div>
        <Button size="sm" loading={cancelling} onClick={onCancel}>
          取消
        </Button>
      </div>
      <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full bg-blue-500 transition-all ${stage === 'enriching' ? 'animate-pulse' : ''}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      <ul className="mt-5 divide-y divide-slate-100 rounded-lg border border-slate-200">
        {tables.map((table) => (
          <li key={table.model_id} className="flex items-center justify-between px-3 py-2 text-xs">
            <span className="text-slate-700">{table.name}</span>
            <Badge
              tone={
                table.status === 'completed'
                  ? 'green'
                  : table.status === 'failed'
                    ? 'red'
                    : table.status === 'running'
                      ? 'blue'
                      : 'slate'
              }
            >
              {{ pending: '等待', running: '进行中', completed: '完成', failed: '失败' }[table.status]}
              {table.attempts > 1 ? ` · 第 ${table.attempts} 次` : ''}
            </Badge>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProposalReview({
  projectId,
  revisionId,
  revisionVersion,
  proposal,
  context,
  onApplied,
}: {
  projectId: string;
  revisionId: string;
  revisionVersion: { expected_etag: number; schema_snapshot_hash: string };
  proposal: AnalyticsModelingProposal;
  context: WorkbenchContext;
  onApplied: () => void;
}) {
  const toast = useToast();
  const spec = context.revision.semantic_spec;
  const modelName = useMemo(() => new Map(spec.models.map((m) => [m.id, m.name])), [spec.models]);
  const fieldName = useMemo(
    () => new Map(spec.fields.map((f) => [f.id, `${modelName.get(f.model_id) ?? ''} · ${f.name}`])),
    [modelName, spec.fields],
  );
  const queryClient = useQueryClient();
  // Seed from the service's defaults: it rejects suggestions that would
  // overwrite human-entered values, and that choice should survive a reload.
  const [rejected, setRejected] = useState<Set<string>>(
    () => new Set(proposal.decisions.filter((d) => !d.accept).map((d) => d.suggestion_id)),
  );
  const seedAliases = () =>
    Object.fromEntries(
      proposal.artifact.alias_drafts.map((draft) => [
        `${draft.resource_type}:${draft.resource_id}`,
        draft.aliases.join('，'),
      ]),
    );
  const [aliases, setAliases] = useState<Record<string, string>>(seedAliases);
  const [seededHash, setSeededHash] = useState(proposal.artifact.artifact_hash);
  if (seededHash !== proposal.artifact.artifact_hash) {
    setSeededHash(proposal.artifact.artifact_hash);
    setAliases(seedAliases());
  }

  const [showEmptyAliases, setShowEmptyAliases] = useState(false);
  const aliasDrafts = proposal.artifact.alias_drafts;
  const emptyValueDrafts = aliasDrafts.filter(
    (d) => d.resource_type === 'dimension_value' && d.aliases.length === 0,
  );
  const visibleDrafts = showEmptyAliases
    ? aliasDrafts
    : aliasDrafts.filter((d) => !(d.resource_type === 'dimension_value' && d.aliases.length === 0));
  const proposalContext = allSemanticContextEntries(proposal.artifact.semantic_context);
  const proposalScopes = useMemo(
    () => buildProposalScopeRows(
      spec,
      proposal.artifact.analysis_topic_datasets,
      proposal.artifact.query_scope_diagnostics,
    ),
    [proposal.artifact.analysis_topic_datasets, proposal.artifact.query_scope_diagnostics, spec],
  );

  const grouped = useMemo(() => {
    const groups: Record<AnalyticsSuggestion['target_kind'], AnalyticsSuggestion[]> = {
      model: [],
      field: [],
      relation: [],
    };
    proposal.suggestions.forEach((s) => groups[s.target_kind].push(s));
    return groups;
  }, [proposal.suggestions]);

  const apply = useMutation({
    mutationFn: async () => {
      const saved = await saveProposal(projectId, revisionId, proposal.id, {
        expected_proposal_etag: proposal.etag,
        expected_proposal_hash: proposal.proposal_hash,
        decisions: proposal.suggestions.map((s) => ({
          suggestion_id: s.id,
          accept: !rejected.has(s.id),
          overrides: {},
        })),
        alias_reviews: proposal.artifact.alias_drafts.map((draft) => ({
          resource_type: draft.resource_type,
          resource_id: draft.resource_id,
          aliases: splitAliases(aliases[`${draft.resource_type}:${draft.resource_id}`] ?? ''),
          display_name: draft.resource_type === 'dimension_value' ? draft.display_name || null : null,
        })),
      });
      // The save advanced the proposal etag server-side; remember it now so a
      // failed apply can be retried without a 409 on the next save.
      queryClient.setQueryData(['proposal', projectId, revisionId, proposal.id], saved);
      if (saved.reviewed_artifact_hash !== saved.artifact.artifact_hash) {
        // Changing decisions regenerated the reviewed catalog artifact; the service
        // requires one more human pass over the new drafts before applying.
        return { regenerated: saved };
      }
      const applied = await applyProposal(projectId, revisionId, proposal.id, {
        ...revisionVersion,
        expected_proposal_etag: saved.etag,
        expected_proposal_hash: saved.proposal_hash,
      });
      return { applied };
    },
    onSuccess: (result) => {
      if ('regenerated' in result) {
        queryClient.setQueryData(['proposal', projectId, revisionId, proposal.id], result.regenerated);
        toast.info('采用的建议变了，目录补全与查询作用域已重新生成，请再核对一遍后采用。');
        return;
      }
      context.acceptRevision(result.applied.revision);
      onApplied();
      toast.success('AI 建模结果已采用。请核对完整语义目录与只读查询作用域。');
      context.goTo('catalog');
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const toggle = (id: string) =>
    setRejected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const section = (title: string, items: AnalyticsSuggestion[], nameOf: (id: string) => string) =>
    items.length > 0 && (
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          {title} {items.length}
        </h3>
        <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {items.map((item) => {
            const lines = Object.entries(item.changes)
              .map(([key, value]) => describeChange(key, value))
              .filter((line): line is string => Boolean(line));
            return (
              <li key={item.id} className="flex items-start gap-3 px-3 py-2 text-xs">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={!rejected.has(item.id)}
                  onChange={() => toggle(item.id)}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-slate-800">
                    <span className="font-medium">{nameOf(item.target_id)}</span>
                    {item.high_impact && <Badge tone="amber">结构性</Badge>}
                    {item.source === 'database_constraint' && <Badge tone="slate">数据库约束</Badge>}
                  </div>
                  <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-slate-600">
                    {lines.map((line) => (
                      <span key={line}>{line}</span>
                    ))}
                  </div>
                  {item.reason && <div className="mt-0.5 text-[11px] text-slate-400">{item.reason}</div>}
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    );

  return (
    <div className="flex flex-col gap-6 px-6 py-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">核对 AI 建模建议</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            取消勾选即不采用该条；别名按「，」或「,」分隔。采用后写入完整语义目录；查询作用域仅作只读编译预览。
          </p>
        </div>
        <Button variant="primary" loading={apply.isPending} onClick={() => apply.mutate()}>
          采用全部已勾选建议
        </Button>
      </div>

      {section('实体', grouped.model, (id) => modelName.get(id) ?? id)}
      {section('字段', grouped.field, (id) => fieldName.get(id) ?? id)}
      {section('关系', grouped.relation, (id) => id)}

      {proposal.artifact.alias_drafts.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            别名 {proposal.artifact.alias_drafts.length}
          </h3>
          <p className="mb-2 text-[11px] text-slate-400">
            用户提问时会按别名匹配指标与维度，例如把「销售额」映射到「订单金额」。每条都需要过目。
            {emptyValueDrafts.length > 0 && (
              <button
                type="button"
                className="ml-2 text-blue-600 hover:text-blue-500"
                onClick={() => setShowEmptyAliases(!showEmptyAliases)}
              >
                {showEmptyAliases ? '隐藏' : '显示'} {emptyValueDrafts.length} 个暂无别名的维度值
              </button>
            )}
          </p>
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
            {visibleDrafts.map((draft) => {
              const key = `${draft.resource_type}:${draft.resource_id}`;
              return (
                <li key={key} className="grid grid-cols-[200px_1fr] items-center gap-3 px-3 py-1.5 text-xs">
                  <div className="min-w-0">
                    <div className="truncate font-medium text-slate-800">{draft.resource_name}</div>
                    <div className="text-[11px] text-slate-400">
                      {{ dimension: '维度', metric: '指标', dimension_value: '维度值' }[draft.resource_type]}
                    </div>
                  </div>
                  <Input
                    className="h-8"
                    value={aliases[key] ?? ''}
                    onChange={(event) => setAliases({ ...aliases, [key]: event.target.value })}
                  />
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section className="grid gap-4 grid-cols-[repeat(auto-fit,minmax(320px,1fr))]">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            默认计数指标 {proposal.artifact.default_count_metrics.length}
          </h3>
          <ul className="rounded-lg border border-slate-200 px-3 py-2 text-xs text-slate-700">
            {proposal.artifact.default_count_metrics.map((m) => (
              <li key={m.id} className="py-0.5">{m.name}</li>
            ))}
            {proposal.artifact.default_count_metrics.length === 0 && (
              <li className="text-slate-400">无（实体缺少已确认主标识）</li>
            )}
          </ul>
        </div>
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            查询作用域编译诊断（只读） {proposalScopes.length}
          </h3>
          <div className="mb-2 rounded-md border border-blue-100 bg-blue-50/60 px-2.5 py-2 text-[11px] text-blue-800">
            <div>编译器 {proposal.artifact.query_scope_compiler_version}</div>
            <div className="mt-0.5 break-all font-mono text-[10px] text-blue-700">
              {proposal.artifact.query_scope_compilation_hash ?? '旧版 artifact 未记录编译哈希'}
            </div>
          </div>
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 px-3 text-xs text-slate-700">
            {proposalScopes.map((scope) => (
              <li key={scope.datasetId} className="py-2">
                <div className="font-medium">{scope.datasetName}</div>
                <div className="mt-1 grid gap-1 text-[11px] text-slate-500">
                  <div><b className="font-medium text-slate-400">事实根</b> {scope.rootName}</div>
                  <div><b className="font-medium text-slate-400">成员实体</b> {scope.modelNames.join('、') || '无'}</div>
                  <div><b className="font-medium text-slate-400">指标</b> {scope.metricNames.join('、') || '无'}</div>
                  <div><b className="font-medium text-slate-400">维度</b> {scope.dimensionNames.join('、') || '无'}</div>
                  <div><b className="font-medium text-slate-400">默认计数</b> {scope.defaultCountName ?? '无（COUNT(*) 拒绝）'}</div>
                  <div><b className="font-medium text-slate-400">关系路径</b> {scope.pathLabels.join('；') || '仅事实根'}</div>
                </div>
                <details className="mt-1.5 rounded bg-slate-50 px-2 py-1 text-[10px]">
                  <summary className="cursor-pointer text-slate-500">规范名称 {scope.canonicalNames.length}</summary>
                  <ul className="mt-1 space-y-0.5 font-mono text-slate-500">
                    {scope.canonicalNames.map(([id, name]) => <li key={id}>{id} → {name}</li>)}
                    {scope.canonicalNames.length === 0 && <li>无</li>}
                  </ul>
                </details>
                <details className="mt-1 rounded bg-slate-50 px-2 py-1 text-[10px]">
                  <summary className="cursor-pointer text-slate-500">编译排除项 {scope.exclusions.length}</summary>
                  <ul className="mt-1 space-y-0.5 text-slate-500">
                    {scope.exclusions.map(([name, reason], index) => (
                      <li key={`${name}:${reason}:${index}`}>{name} · {reason}</li>
                    ))}
                    {scope.exclusions.length === 0 && <li>无</li>}
                  </ul>
                </details>
              </li>
            ))}
            {proposalScopes.length === 0 && (
              <li className="py-2 text-slate-400">
                尚未生成兼容查询作用域；完整语义目录仍会保留。
              </li>
            )}
          </ul>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          语义上下文（只读） {proposalContext.length}
        </h3>
        <p className="mb-2 text-[11px] text-slate-400">
          每条上下文保留目标、类型与来源；确认后随版本冻结，问数时只使用与所选作用域相关的条目。
        </p>
        <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {proposalContext.map((entry) => (
            <li key={entry.id} className="px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="blue">{entry.target_type}</Badge>
                <Badge tone="violet">{entry.kind}</Badge>
                <Badge tone="slate">{entry.source_type}</Badge>
                <span className="font-mono text-[10px] text-slate-400">{entry.target_id}</span>
              </div>
              <div className="mt-1 whitespace-pre-wrap leading-relaxed text-slate-600">{entry.text}</div>
              {entry.source_ref && (
                <div className="mt-1 break-all font-mono text-[10px] text-slate-400">{entry.source_ref}</div>
              )}
            </li>
          ))}
          {proposalContext.length === 0 && (
            <li className="px-3 py-2 text-xs text-slate-400">本次建议没有新增已审核语义上下文。</li>
          )}
        </ul>
      </section>
    </div>
  );
}

function splitAliases(text: string): string[] {
  const seen = new Set<string>();
  return text
    .split(/[，,、\n]/)
    .map((item) => item.trim())
    .filter((item) => {
      const key = item.toLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}
