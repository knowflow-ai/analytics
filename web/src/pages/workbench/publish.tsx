import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, Circle, Info, Rocket, Send, ShieldCheck } from 'lucide-react';
import { useMemo, useState, type FormEvent } from 'react';
import { Link } from '@analytics/lib/router';
import {
  getCurrentEvaluation,
  getCurrentQualityReport,
  getDiagnostics,
  listGoldenSuites,
  listQueryFailures,
  listReleases,
  rollbackRelease,
  saveGoldenSuite,
  newResourceId,
  previewQuery,
  publishRevision,
  validateRevision,
  versionOf,
  type QueryInput,
} from '@analytics/api/analytics';
import type { AnalyticsClarificationOption, AnalyticsQueryResponse } from '@analytics/api/types';
import {
  buildClarificationContinuation,
  QueryAnswer,
  type QueryTurn,
} from '@analytics/components/query-answer';
import { Badge, Button, Empty, Spinner, useToast } from '@analytics/components/ui';
import { describeError, formatDateTime } from '@analytics/lib/labels';
import type { WorkbenchContext } from './index';
import { GoldenSuiteCard } from './golden-suite-card';
import { QualityReportCard } from './quality-report-card';
import { StructuredTrial } from './structured-trial';
import { appPath } from '@analytics/api/edition';

export function PublishPanel({ projectId, revision, acceptRevision, readOnly }: WorkbenchContext) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const diagnostics = useQuery({
    queryKey: ['diagnostics', projectId, revision.id, revision.etag],
    queryFn: () => getDiagnostics(projectId, revision.id),
  });
  const releases = useQuery({ queryKey: ['releases', projectId], queryFn: () => listReleases(projectId) });
  // 质量报告按语义 id 说话;翻成业务名,用户才知道「哪个模型/关系/指标」。
  const semanticNames = useMemo(() => {
    const spec = revision.semantic_spec;
    const entries: Array<[string, string]> = [];
    for (const item of spec.models) entries.push([item.id, item.name]);
    for (const item of spec.dimensions) entries.push([item.id, item.name]);
    for (const item of spec.metrics) entries.push([item.id, item.name]);
    for (const item of spec.relations) {
      const left = spec.models.find((m) => m.id === item.left_model_id)?.name ?? item.left_model_id;
      const right =
        spec.models.find((m) => m.id === item.right_model_id)?.name ?? item.right_model_id;
      entries.push([item.id, `${left} → ${right}`]);
    }
    return new Map(entries);
  }, [revision.semantic_spec]);

  const validate = useMutation({
    mutationFn: () => validateRevision(projectId, revision.id),
    onSuccess: (next) => {
      acceptRevision(next);
      toast.success('校验通过，可以发布。');
    },
    onError: (error) => {
      toast.error(describeError(error));
      diagnostics.refetch();
    },
  });
  const rollback = useMutation({
    mutationFn: () => rollbackRelease(projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['releases', projectId] });
      queryClient.invalidateQueries({ queryKey: ['summary', projectId] });
      toast.success('已回滚,线上问数现在使用上一版。');
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const publish = useMutation({
    mutationFn: () => publishRevision(projectId, revision.id, versionOf(revision)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['summary', projectId] });
      queryClient.invalidateQueries({ queryKey: ['releases', projectId] });
      queryClient.invalidateQueries({ queryKey: ['revision', projectId, revision.id] });
      toast.success('已发布。现在可以用自然语言提问了。');
    },
    onError: (error) => toast.error(describeError(error)),
  });

  // 发布门禁要求质量报告存在且 ready(无阻断 且 无待核对)。此前按钮只看
  // revision.state,显示「可发布」但后端 409 拒绝——用户看到的是「点了没反应」。
  const quality = useQuery({
    queryKey: ['quality-report', projectId, revision.id, revision.etag],
    queryFn: () => getCurrentQualityReport(projectId, revision.id),
  });
  const report = quality.data?.report ?? null;
  const pendingReviewCount = (report?.metric_previews ?? []).filter(
    (item) => item.status === 'pending_review',
  ).length;
  const qualityReady = Boolean(report?.ready);
  const evaluation = useQuery({
    queryKey: ['evaluation-latest', projectId, revision.id, revision.etag],
    queryFn: () => getCurrentEvaluation(projectId, revision.id),
  });
  const evalReport = evaluation.data?.report ?? null;
  const evalReady = Boolean(evalReport?.gate_passed);
  // 发布门禁有三道:结构校验、数据质量、黄金评测。此前界面一道都没体现,
  // 用户只能靠点发布撞 409 才知道差什么。
  const publishTasks = [
    {
      key: 'quality',
      label: '数据质量校验',
      done: qualityReady,
      hint: qualityReady
        ? '已核对'
        : report === null
          ? '尚未核对'
          : report.blocking_count > 0
            ? `${report.blocking_count} 个阻断问题`
            : `${pendingReviewCount} 项指标样本待确认`,
    },
    {
      key: 'evaluation',
      label: '评测集',
      done: evalReady,
      hint: evalReady
        ? `${evalReport?.total} 条用例全部通过`
        : evalReport === null
          ? '尚未运行评测'
          : `${evalReport.passed}/${evalReport.total} 通过`,
    },
  ];
  const publishReady = publishTasks.every((task) => task.done);

  const blocking = diagnostics.data?.diagnostics.filter((d) => d.blocking) ?? [];
  const warnings = diagnostics.data?.diagnostics.filter((d) => !d.blocking) ?? [];
  const validated = revision.state === 'validated';

  return (
    <div className="grid min-h-[560px] grid-cols-[1fr_320px]">
      <section className="px-6 py-5">
        <h2 className="text-sm font-semibold text-slate-900">校验与发布</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          校验检查关系基数、指标覆盖、查询作用域的唯一安全路径与默认计数绑定；发布会冻结完整目录并建立语义索引。
        </p>

        <div className="mt-5 flex items-center gap-3 rounded-lg border border-slate-200 p-4">
          <span
            className={`grid h-9 w-9 place-items-center rounded-lg ${
              validated || revision.state === 'published'
                ? 'bg-emerald-50 text-emerald-600'
                : blocking.length
                  ? 'bg-red-50 text-red-600'
                  : 'bg-slate-100 text-slate-500'
            }`}
          >
            <ShieldCheck className="h-5 w-5" />
          </span>
          <div className="flex-1 text-xs">
            <div className="font-medium text-slate-800">
              {revision.state === 'published'
                ? '本版本已发布'
                : validated
                  ? publishReady
                    ? '已校验，可发布'
                    : '已校验，还有发布前检查未完成'
                  : diagnostics.data?.ready
                    ? '无阻断问题，可以校验'
                    : `${blocking.length} 个阻断问题`}
            </div>
            <div className="text-slate-400">v{revision.etag} · 快照 {revision.schema_snapshot_hash.slice(0, 10)}</div>
          </div>
          {!readOnly && !validated && (
            <Button loading={validate.isPending} onClick={() => validate.mutate()}>
              校验
            </Button>
          )}
          {validated && (
            <Button variant="primary" icon={<Rocket className="h-4 w-4" />} loading={publish.isPending}
              disabled={!publishReady} onClick={() => publish.mutate()}>
              发布
            </Button>
          )}
          {revision.state === 'published' && (
            <Link to={appPath(`/projects/${projectId}/ask`)}>
              <Button variant="primary">开始问数</Button>
            </Link>
          )}
        </div>

        {validated && revision.state !== 'published' && (
          <ul className="mt-3 flex flex-col gap-2 rounded-lg border border-slate-200 p-4">
            <li className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              发布前检查
            </li>
            {publishTasks.map((task) => (
              <li key={task.key} className="flex items-center gap-2 text-xs">
                {task.done ? (
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
                ) : (
                  <Circle className="h-4 w-4 shrink-0 text-slate-300" />
                )}
                <span className={task.done ? 'text-slate-500' : 'font-medium text-slate-800'}>
                  {task.label}
                </span>
                <span className="ml-auto text-slate-400">{task.hint}</span>
              </li>
            ))}
          </ul>
        )}

        {diagnostics.isPending && <Spinner />}
        {diagnostics.isError && <div className="mt-4 text-xs text-red-600">{describeError(diagnostics.error)}</div>}
        {diagnostics.data && (
          <div className="mt-5 flex flex-col gap-4">
            {diagnostics.data.diagnostics.length === 0 && (
              <div className="flex items-center gap-2 text-xs text-emerald-600">
                <CheckCircle2 className="h-4 w-4" /> 没有发现问题。
              </div>
            )}
            {[...blocking, ...warnings].map((item, index) => (
              <div
                key={`${item.diagnostic_code}-${index}`}
                className={`rounded-lg border p-3 text-xs ${
                  item.blocking ? 'border-red-200 bg-red-50/50' : 'border-slate-200'
                }`}
              >
                <div className="flex items-center gap-2">
                  {item.blocking ? (
                    <AlertTriangle className="h-4 w-4 text-red-600" />
                  ) : (
                    <Info className="h-4 w-4 text-slate-400" />
                  )}
                  <span className="font-medium text-slate-800">{item.title}</span>
                  <Badge tone={item.blocking ? 'red' : 'slate'}>{item.blocking ? '阻断' : '提醒'}</Badge>
                </div>
                <div className="mt-1 text-slate-600">{item.message}</div>
                {item.recommended_action && (
                  <div className="mt-1 text-[11px] text-slate-400">建议：{item.recommended_action}</div>
                )}
              </div>
            ))}
          </div>
        )}
        <QualityReportCard
          projectId={projectId}
          revision={revision}
          names={semanticNames}
          readOnly={readOnly}
        />
        <TrialQuestions projectId={projectId} revision={revision} />
        <GoldenSuiteCard projectId={projectId} revision={revision} readOnly={readOnly} />
      </section>

      <aside className="border-l border-slate-100 px-4 py-5">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">发布历史</div>
        {releases.isPending && <Spinner />}
        {releases.data && releases.data.items.length === 0 && (
          <div className="text-xs text-slate-400">尚未发布过。</div>
        )}
        <ul className="flex flex-col gap-2">
          {releases.data?.items.map((release) => (
            <li key={release.id} className="rounded-md border border-slate-200 px-3 py-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-mono text-slate-600">{release.id.slice(0, 14)}</span>
                <Badge tone={release.status === 'active' ? 'green' : 'slate'}>
                  {release.status === 'active' ? '线上' : '历史'}
                </Badge>
              </div>
              <div className="mt-0.5 text-slate-400">{formatDateTime(release.created_at)}</div>
              {release.status === 'active' && (releases.data?.items.length ?? 0) > 1 && (
                <button
                  type="button"
                  className="mt-1 text-[11px] text-slate-400 underline hover:text-red-600"
                  onClick={() => {
                    if (window.confirm('回滚到上一版?线上问数会立即切换,当前版本进入历史。'))
                      rollback.mutate();
                  }}
                >
                  回滚到上一版
                </button>
              )}
            </li>
          ))}
        </ul>
        <QueryFailuresCard projectId={projectId} />
      </aside>
    </div>
  );
}


/**
 * Ask the unpublished candidate directly. Same core path as the live query
 * except it binds to this revision, so a wrong answer here is a modeling
 * problem, not a release problem.
 */
function TrialQuestions({ projectId, revision }: Pick<WorkbenchContext, 'projectId' | 'revision'>) {
  const spec = revision.semantic_spec;
  const names = useMemo(() => {
    const map = new Map<string, string>();
    spec.dimensions.forEach((d) => map.set(d.id, d.name));
    spec.metrics.forEach((m) => map.set(m.id, m.name));
    spec.datasets.forEach((d) => map.set(d.id, d.name));
    return map;
  }, [spec]);
  const nameOf = (id: string) => names.get(id) ?? id.split(':').pop() ?? id;
  const [tab, setTab] = useState<'nl' | 'structured'>('nl');
  const queryClient = useQueryClient();
  const toast = useToast();
  const [savedTurns, setSavedTurns] = useState<Set<string>>(new Set());
  // 期望字段有二十多个,手填是灾难;唯一入口是「答对了就存」,期望取本次
  // 真实解析结果(dataset/query_type/metrics/dimensions + COMPLETED)。
  const saveCase = useMutation({
    mutationFn: async (turn: QueryTurn) => {
      const response = turn.response;
      if (!response || response.state !== 'COMPLETED') return;
      const existing = (await listGoldenSuites(projectId, revision.id)).items[0]?.suite;
      const suite = existing ?? {
        id: newResourceId('suite'),
        name: '默认评测集',
        project_id: projectId,
        // 评测重放用套件级固定时钟:「近7天」「8月2日」这类问题的确定性
        // 边界随 now 变化,不固定的话隔天跑评测就全线漂移。
        fixed_now: new Date().toISOString(),
        cases: [],
      };
      const normalized = turn.question.trim();
      if (suite.cases.some((item) => item.question.trim() === normalized)) return;
      await saveGoldenSuite(projectId, revision.id, versionOf(revision), {
        ...suite,
        cases: [
          ...suite.cases,
          {
            id: newResourceId('case'),
            question: normalized,
            dataset_ids: [response.interpretation.dataset_id],
            tags: [],
            // 存入本身就是人工确认「这个答案对」:确认过的用例即成为少样本示例,
            // 相似的新问题直接参考已确认的语义合同(服务端四重资格门仍然生效)。
            memory_status: 'ENABLED',
            memory_review_result: 'POSITIVE',
            memory_review_comment: '试问答对后由用户存入',
            expected_state: 'COMPLETED',
            expected_dataset_id: response.interpretation.dataset_id,
            expected_query_type: response.interpretation.query_type,
            expected_metric_ids: response.semantic_query.metric_ids,
            expected_aggregation_overrides: response.semantic_query.aggregation_overrides,
            expected_dimension_ids: response.semantic_query.dimension_ids,
            // 期望必须照实取本次解析的全量语义,漏一类就是一类假失败:
            // 第一版把 filters 写死为空,带时间条件的问题一存就
            // 「semantic projection differs」(用户实测)。
            expected_filters: response.semantic_query.filters,
            expected_measure_filters: response.semantic_query.measure_filters,
            expected_metric_filters: response.semantic_query.metric_filters,
            // 照实存全量:比较端只在有 limit(top-N)时才严格比较排序,
            // 无 limit 的装饰性 ORDER BY 不再产生假失败,存端无需再做条件裁剪。
            expected_order_by: response.semantic_query.order_by,
            expected_limit: response.semantic_query.limit ?? undefined,
            // 契约要求 COMPLETED 用例必须带结果行(实测被拒才发现);没有显式
            // 排序时行序不稳定,不按序比对。
            expected_rows: response.data.rows,
            // 行序只在 top-N 下有语义;无 limit 时行序随 ORDER BY 是否出现而波动。
            row_order_matters:
              (response.semantic_query.order_by?.length ?? 0) > 0 &&
              response.semantic_query.limit != null,
            numeric_tolerance: '0.000001',
          },
        ],
      });
      return turn.id;
    },
    onSuccess: (turnId) => {
      if (!turnId) return;
      setSavedTurns((prev) => new Set(prev).add(turnId));
      queryClient.invalidateQueries({ queryKey: ['golden', projectId, revision.id] });
      toast.success('已存为评测用例。');
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const [question, setQuestion] = useState('');
  const [turns, setTurns] = useState<QueryTurn[]>([]);
  const ask = useMutation({
    mutationFn: ({ turnId, input }: { turnId: string; input: QueryInput }) =>
      previewQuery(projectId, revision.id, versionOf(revision), input).then((response) => ({ turnId, response })),
    onSuccess: ({ turnId, response }) =>
      setTurns((c) => c.map((t) => (t.id === turnId ? { ...t, response, pending: false } : t))),
    onError: (error, { turnId }) =>
      setTurns((c) => c.map((t) => (t.id === turnId ? { ...t, error: describeError(error), pending: false } : t))),
  });
  const submit = (text: string, extra: Partial<QueryInput> = {}) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const turnId = newResourceId('turn');
    const input: QueryInput = {
      question: trimmed,
      dataset_ids: spec.datasets.map((scope) => scope.id),
      ...extra,
    };
    setTurns((c) => [
      { id: turnId, question: trimmed, input, pending: true },
      ...c,
    ]);
    setQuestion('');
    ask.mutate({ turnId, input });
  };
  const choose = (option: AnalyticsClarificationOption, response: AnalyticsQueryResponse) => {
    const origin = turns.find((t) => t.response === response);
    if (!origin?.input) return;
    submit(
      origin.question,
      buildClarificationContinuation(origin.input, option, response),
    );
  };
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit(question);
  };
  if (spec.datasets.length === 0) {
    return (
      <Empty
        title="当前版本尚不可试问"
        hint="完整语义目录仍然有效；请运行 AI 建模并完成验证。"
      />
    );
  }
  return (
    <div className="mt-8">
      <div className="flex items-center gap-4">
        <h3 className="text-sm font-semibold text-slate-900">发布前试问</h3>
        <div className="flex rounded-md border border-slate-200 p-0.5 text-xs">
          {(
            [
              ['nl', '自然语言'],
              ['structured', '结构化'],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={`rounded px-2.5 py-1 ${tab === key ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <p className="mt-0.5 text-xs text-slate-400">
        {tab === 'nl'
          ? '对当前完整语义目录直接提问，走与线上完全相同的解析与翻译链路；系统只在业务含义无法唯一判断时请求确认。'
          : '直接选指标、维度和过滤提交语义查询，跳过自然语言解析；这里失败就是建模或 SQL 问题，与别名无关。'}
      </p>
      {tab === 'structured' && (
        <div className="mt-3">
          <StructuredTrial projectId={projectId} revision={revision} />
        </div>
      )}
      <form onSubmit={onSubmit} className={`mt-3 flex items-center gap-2 ${tab === 'nl' ? '' : 'hidden'}`}>
        <input
          className="h-9 flex-1 rounded-md border border-slate-200 bg-white px-3 text-[13px] placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100"
          placeholder="例如：各地区的订单金额是多少？"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <Button type="submit" variant="primary" icon={<Send className="h-3.5 w-3.5" />} disabled={!question.trim()}>
          试问
        </Button>
      </form>
      <div className={`mt-4 flex flex-col gap-4 ${tab === 'nl' ? '' : 'hidden'}`}>
        {turns.map((turn) => (
          <div key={turn.id} className="rounded-lg border border-slate-200 p-3">
            <div className="mb-2 flex items-center justify-between gap-2 text-xs font-medium text-slate-700">
              <span>Q：{turn.question}</span>
              {turn.response?.state === 'COMPLETED' && (
                <button
                  type="button"
                  disabled={saveCase.isPending || savedTurns.has(turn.id)}
                  onClick={() => saveCase.mutate(turn)}
                  className="shrink-0 text-[11px] font-normal text-slate-400 hover:text-blue-600 disabled:opacity-60"
                >
                  {savedTurns.has(turn.id) ? '已存为评测用例' : '答对了？存为评测用例'}
                </button>
              )}
            </div>
            <QueryAnswer projectId={projectId} turn={turn} columnName={nameOf} onChoose={choose} />
          </div>
        ))}
      </div>
    </div>
  );
}


/**
 * 没答上的问题:系统「听不懂什么」的一手数据。此前只写不读——别名缺口、
 * 术语挖掘、黄金问题种子都没有输入。列在发布历史下方,发布后回来看一眼
 * 就知道该补哪些说法。
 */
function QueryFailuresCard({ projectId }: { projectId: string }) {
  const failures = useQuery({
    queryKey: ['query-failures', projectId],
    queryFn: () => listQueryFailures(projectId, 50),
  });
  const items = failures.data?.items ?? [];
  if (failures.isPending || items.length === 0) return null;
  return (
    <div className="mt-5">
      <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        没答上的问题 {items.length}
      </div>
      <ul className="flex flex-col gap-1.5">
        {items.slice(0, 20).map((item, index) => (
          <li key={index} className="rounded-md border border-slate-200 px-2.5 py-1.5 text-[11px]">
            <div className="text-slate-700">「{item.question}」</div>
            <div className="mt-0.5 text-slate-400">{item.message || item.code}</div>
          </li>
        ))}
      </ul>
      <div className="mt-1.5 text-[10px] leading-relaxed text-slate-400">
        多数是说法没被别名覆盖。到对应实体的指标 / 维度里把这些说法补成别名,下次就能答上。
      </div>
    </div>
  );
}
