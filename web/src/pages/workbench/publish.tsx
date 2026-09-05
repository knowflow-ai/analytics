import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ChevronRight,
  Info,
  Loader2,
  RefreshCw,
  Rocket,
  Send,
  ShieldCheck,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  createQualityReport,
  getCurrentEvaluation,
  getCurrentQualityReport,
  getDiagnostics,
  listGoldenSuites,
  listQueryFailures,
  listReleases,
  activateRelease,
  saveGoldenSuite,
  newResourceId,
  previewQuery,
  publishRevision,
  reviewQualityReport,
  validateRevision,
  versionOf,
  type QueryInput,
} from '@analytics/api/analytics';
import type {
  AnalyticsClarificationOption,
  AnalyticsQueryResponse,
  AnalyticsReleaseSummary,
} from '@analytics/api/types';
import {
  buildClarificationContinuation,
  QueryAnswer,
  type QueryTurn,
} from '@analytics/components/query-answer';
import {
  Badge,
  Button,
  ConfirmationDialog,
  Empty,
  Spinner,
  useToast,
} from '@analytics/components/ui';
import { feedbackRows } from './ask-feedback-state';
import { describeError, formatDateTime } from '@analytics/lib/labels';
import type { WorkbenchContext } from './index';
import { GoldenSuiteCard } from './golden-suite-card';
import {
  autoPassedCount,
  publishChecks,
  publishHeadline,
  publishReady,
  qualityReportIsFresh,
  reviewQueue,
  shouldAutoRunQuality,
  shouldAutoValidate,
  type CheckState,
  type PublishCheck,
  type ReviewItem,
} from './publish-checks';
import { QualityReportCard } from './quality-report-card';
import { StructuredTrial } from './structured-trial';
import { ANALYTICS_TASK_PANEL_CLASS, scrollableAncestor } from '@analytics/lib/layout';

const CHECK_BOX: Record<CheckState, string> = {
  queued: 'border-slate-200 bg-white',
  running: 'border-blue-200 bg-blue-50/60',
  passed: 'border-slate-200 bg-white',
  attention: 'border-amber-200 bg-amber-50/50',
  blocked: 'border-red-200 bg-red-50/50',
};
const CHECK_TEXT: Record<CheckState, string> = {
  queued: 'text-slate-400',
  running: 'text-blue-700',
  passed: 'text-emerald-700',
  attention: 'text-amber-700',
  blocked: 'text-red-700',
};
const CHECK_DOT: Record<CheckState, string> = {
  queued: 'border-[1.5px] border-slate-300',
  running: 'border-2 border-blue-200 border-t-blue-600 animate-spin',
  passed: 'bg-emerald-500',
  attention: 'bg-amber-500',
  blocked: 'bg-red-600',
};
const HEAD_ICON = {
  running: 'bg-blue-50 text-blue-600',
  blocked: 'bg-red-50 text-red-600',
  attention: 'bg-amber-50 text-amber-600',
  ok: 'bg-emerald-50 text-emerald-600',
};

/**
 * 问数验证：能自己跑的自己跑。
 *
 * 原来结构校验和数据质量都要手点，不点就没有结论——而发布门禁读的正是这两份结论，
 * 于是「发布」按钮亮着、点下去后端 409，用户看到的是「点了没反应」。这两件事机器
 * 完全判得了（一个只读当前草稿，一个对数据库做只读检查），让人点只是把机器的活儿
 * 摊给了人。判定住在 `publish-checks.ts`，这里只负责触发和显示。
 *
 * 评测集不自动跑：重放会把每条用例跑一遍完整问数链路（含模型调用），进一次页面就
 * 花掉一次预算，不该是默认行为。
 */
export function PublishPanel({
  projectId,
  revision,
  acceptRevision,
  readOnly,
  goTo,
  trialQuestion,
  onTrialQuestionHandled,
}: WorkbenchContext & {
  /** 从「问数反馈」的点赞行跳过来时要试问的那句话。 */
  trialQuestion?: string | null;
  onTrialQuestionHandled?: () => void;
}) {
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

  const qualityKey = ['quality-report', projectId, revision.id, revision.etag];
  const quality = useQuery({
    queryKey: qualityKey,
    queryFn: () => getCurrentQualityReport(projectId, revision.id),
  });
  const evaluation = useQuery({
    queryKey: ['evaluation-latest', projectId, revision.id, revision.etag],
    queryFn: () => getCurrentEvaluation(projectId, revision.id),
  });

  const [structureError, setStructureError] = useState<string | null>(null);
  const [showPassed, setShowPassed] = useState(false);
  /** 待确认要切到哪一版；null 表示没有待确认的切换。 */
  const [switchingTo, setSwitchingTo] = useState<AnalyticsReleaseSummary | null>(null);
  // 评测集是三道检查里唯一要人主动跑的（重放会花掉一次模型预算）。格子里没有入口
  // 的话，用户得自己滚到页面最底下才找得到那张卡。
  const evaluationRef = useRef<HTMLDivElement>(null);
  // 自动跑过一次就不再跑，按「哪一版」记账：草稿一改 etag 就变，自然会重跑。
  const autoRan = useRef<{ validate: string | null; quality: string | null }>({
    validate: null,
    quality: null,
  });
  const versionKey = `${revision.id}:${revision.etag}`;

  const validate = useMutation({
    mutationFn: () => validateRevision(projectId, revision.id),
    onSuccess: (next) => {
      setStructureError(null);
      acceptRevision(next);
    },
    onError: (error) => {
      // 自动跑失败不弹 toast：进页面就甩一个红条，等于用报错代替了本来该好好显示的
      // 诊断列表。原因写进结构校验那一格。
      setStructureError(describeError(error));
      diagnostics.refetch();
    },
  });
  const runQuality = useMutation({
    mutationFn: () => createQualityReport(projectId, revision.id, versionOf(revision)),
    onSuccess: (next) => queryClient.setQueryData(qualityKey, { report: next }),
  });
  const review = useMutation({
    mutationFn: (decisions: Array<{ preview_id: string; confirm: boolean }>) =>
      reviewQualityReport(projectId, revision.id, quality.data!.report!, decisions),
    onSuccess: (next) => queryClient.setQueryData(qualityKey, { report: next }),
    onError: (error) => toast.error(describeError(error)),
  });
  const switchRelease = useMutation({
    mutationFn: (release: AnalyticsReleaseSummary) => activateRelease(projectId, release.id),
    onSuccess: (_data, release) => {
      queryClient.invalidateQueries({ queryKey: ['releases', projectId] });
      queryClient.invalidateQueries({ queryKey: ['summary', projectId] });
      toast.success(`线上问数现在使用第 ${release.sequence} 版。`);
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

  const report = quality.data?.report ?? null;
  const freshReport = qualityReportIsFresh(report, revision.etag) ? report : null;

  useEffect(() => {
    const ok = shouldAutoValidate({
      revisionState: revision.state,
      readOnly,
      diagnosticsLoaded: Boolean(diagnostics.data),
      diagnosticsReady: Boolean(diagnostics.data?.ready),
      alreadyTried: autoRan.current.validate === versionKey,
    });
    if (!ok) return;
    autoRan.current.validate = versionKey;
    validate.mutate();
    // validate 是稳定的 mutation 对象，放进依赖只会让 effect 白跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diagnostics.data, readOnly, revision.state, versionKey]);

  useEffect(() => {
    const ok = shouldAutoRunQuality({
      revisionState: revision.state,
      readOnly,
      report: quality.isPending ? undefined : report,
      revisionEtag: revision.etag,
      alreadyTried: autoRan.current.quality === versionKey,
    });
    if (!ok) return;
    autoRan.current.quality = versionKey;
    runQuality.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quality.isPending, readOnly, report, revision.etag, revision.state, versionKey]);

  const blocking = diagnostics.data?.diagnostics.filter((d) => d.blocking) ?? [];
  const warnings = diagnostics.data?.diagnostics.filter((d) => !d.blocking) ?? [];

  const checks = publishChecks({
    revisionState: revision.state,
    diagnosticsLoaded: Boolean(diagnostics.data),
    blockingCount: blocking.length,
    structureRunning: validate.isPending,
    structureError,
    qualityRunning: runQuality.isPending,
    report: freshReport,
    revisionEtag: revision.etag,
    evaluation: evaluation.data?.report ?? null,
  });
  const queue = reviewQueue({
    diagnostics: diagnostics.data?.diagnostics ?? [],
    report: freshReport,
    names: semanticNames,
  });
  const ready = publishReady(checks, queue) && !readOnly;
  const autoPassed = autoPassedCount(freshReport, queue);
  const headline = publishHeadline({
    checks,
    queue,
    revisionState: revision.state,
    autoPassed,
  });
  const busy = checks.some((check) => check.state === 'running');
  const pendingPreviews = queue.filter((item) => item.previewId && !item.rejected);

  /**
   * 手动重跑。一般用不上——改了建模 etag 就变，自动跑会重新来一遍。
   *
   * 把手动跑的那一项记成「这一版已经跑过」：否则 `diagnostics.refetch()` 回来后
   * 自动跑的判定又成立，同一次点击会发两遍请求。
   */
  /**
   * 滚到评测集那张卡。
   *
   * 不用 `scrollIntoView`：工作台外面那张卡是裁剪容器，scrollIntoView 会连它一起滚，
   * 把整页内容顶上去且滚不回来（用户实测「拉不下去」）。这里只滚真正带滚动条的祖先。
   */
  const scrollToEvaluation = () => {
    const node = evaluationRef.current;
    if (!node) return;
    const scroller = scrollableAncestor(node);
    if (!scroller) return;
    const top =
      node.getBoundingClientRect().top -
      scroller.getBoundingClientRect().top +
      scroller.scrollTop -
      24;
    scroller.scrollTo({ top, behavior: 'smooth' });
  };

  const rerun = () => {
    setStructureError(null);
    autoRan.current = { validate: null, quality: null };
    diagnostics.refetch();
    if (readOnly) return;
    const withToast = { onError: (error: unknown) => toast.error(describeError(error)) };
    if (revision.state === 'draft') {
      autoRan.current.validate = versionKey;
      validate.mutate(undefined, withToast);
    } else {
      autoRan.current.quality = versionKey;
      runQuality.mutate(undefined, withToast);
    }
  };

  return (
    <div
      className={`grid h-full grid-cols-[minmax(0,1fr)_320px] ${ANALYTICS_TASK_PANEL_CLASS}`}
    >
      <section className="min-w-0 px-6 py-5">
        {/* 一、检查总条：自动跑，不需要点 */}
        <div className="rounded-lg border border-slate-200 p-4">
          <div className="flex items-start gap-3">
            <span
              className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg ${HEAD_ICON[headline.tone]}`}
            >
              {headline.tone === 'running' ? (
                <Loader2 className="h-5 w-5 animate-spin" />
              ) : headline.tone === 'ok' ? (
                <ShieldCheck className="h-5 w-5" />
              ) : (
                <AlertTriangle className="h-5 w-5" />
              )}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-semibold text-slate-900">{headline.title}</div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-slate-400">
                {headline.sub}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-400">
                v{revision.etag} · 快照 {revision.schema_snapshot_hash.slice(0, 10)}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {!readOnly && (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<RefreshCw className="h-3.5 w-3.5" />}
                  loading={busy}
                  onClick={rerun}
                  title="改了建模会自动重跑，一般不需要点"
                >
                  重跑
                </Button>
              )}
              {/* 问数入口不在建模工作台里：开源版只做语义建模与发布前验证，
                  问数助手与报表是商业版的产品面。 */}
              {revision.state !== 'published' && (
                <Button
                  size="sm"
                  variant={ready ? 'primary' : 'default'}
                  icon={<Rocket className="h-3.5 w-3.5" />}
                  loading={publish.isPending}
                  disabled={!ready}
                  onClick={() => publish.mutate()}
                >
                  发布
                </Button>
              )}
            </div>
          </div>

          <div className="mt-3.5 flex items-stretch gap-2">
            {checks.map((check) => (
              <CheckCell
                key={check.key}
                check={check}
                onGo={
                  check.key === 'evaluation' && check.state !== 'passed'
                    ? scrollToEvaluation
                    : undefined
                }
              />
            ))}
          </div>

          {/* 自动校验失败时原因不能只剩一个「未通过」——那等于把错误咽掉了。 */}
          {structureError && (
            <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              结构校验没通过：{structureError}
            </div>
          )}
        </div>

        {/* 二、只有人能拍板的 */}
        {queue.length > 0 && !busy && (
          <div className="mt-5">
            <div className="mb-2 flex items-center gap-2">
              <h3 className="text-[13px] font-semibold text-slate-900">需要你确认</h3>
              <Badge tone="amber">{queue.length}</Badge>
              <span className="text-[11px] text-slate-400">
                机器判得了的都判完了，剩下的是只有人能拍板的
              </span>
              {pendingPreviews.length > 1 && !readOnly && (
                <Button
                  size="sm"
                  className="ml-auto"
                  loading={review.isPending}
                  onClick={() =>
                    review.mutate(
                      pendingPreviews.map((item) => ({ preview_id: item.previewId!, confirm: true })),
                    )
                  }
                >
                  全部数值正确
                </Button>
              )}
            </div>
            <ul className="overflow-hidden rounded-lg border border-slate-200">
              {queue.map((item) => (
                <QueueRow
                  key={item.id}
                  item={item}
                  readOnly={readOnly}
                  busy={review.isPending}
                  onDecide={(confirm) =>
                    review.mutate([{ preview_id: item.previewId!, confirm }])
                  }
                />
              ))}
            </ul>
          </div>
        )}

        {/* 三、自动通过的检查：默认折叠 */}
        <div className="mt-3 rounded-lg border border-slate-200">
          <button
            type="button"
            aria-expanded={showPassed}
            onClick={() => setShowPassed(!showPassed)}
            className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
          >
            <ChevronRight
              className={`h-3.5 w-3.5 text-slate-400 transition-transform ${showPassed ? 'rotate-90' : ''}`}
            />
            <span className="text-xs text-slate-600">自动通过的检查</span>
            <span className="text-xs text-slate-400">{autoPassed} 项</span>
            <span className="ml-auto text-[11px] text-slate-400">
              {showPassed ? '收起' : '一般不用看'}
            </span>
          </button>
          {showPassed && (
            <div className="border-t border-slate-100 px-4 py-3">
              {warnings.length > 0 && (
                <div className="mb-3 flex flex-col gap-2">
                  {warnings.map((item, index) => (
                    <div
                      key={`${item.diagnostic_code}-${index}`}
                      className="rounded-md border border-slate-200 p-2.5 text-xs"
                    >
                      <div className="flex items-center gap-2">
                        <Info className="h-3.5 w-3.5 text-slate-400" />
                        <span className="font-medium text-slate-700">{item.title}</span>
                        <Badge tone="slate">提醒</Badge>
                      </div>
                      <div className="mt-1 text-slate-500">{item.message}</div>
                    </div>
                  ))}
                </div>
              )}
              <QualityReportCard report={freshReport} names={semanticNames} />
            </div>
          )}
        </div>

        <TrialQuestions
          projectId={projectId}
          revision={revision}
          trialQuestion={trialQuestion}
          onTrialQuestionHandled={onTrialQuestionHandled}
        />
        <div ref={evaluationRef}>
          <GoldenSuiteCard projectId={projectId} revision={revision} readOnly={readOnly} />
        </div>
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
                {/* 完整 id 排障时才需要，留在 title 里；平时读"第几版"就够了。 */}
                <span className="font-medium text-slate-700" title={release.id}>
                  第 {release.sequence} 版
                </span>
                <Badge tone={release.status === 'active' ? 'green' : 'slate'}>
                  {release.status === 'active' ? '线上' : '历史'}
                </Badge>
              </div>
              <div className="mt-0.5 text-slate-400">{formatDateTime(release.created_at)}</div>
              {/*
                入口挂在每个非线上版本上，而不是挂在线上那一行说"回滚到上一版"：
                后者只能往更早走，走过头就回不来了——线上停在第 1 版时，第 2 版仍
                列在这里却没有任何入口能切回去。已发布版本是不可变快照，切到哪一
                版都只是改一个指针。
              */}
              {release.status !== 'active' && (
                <button
                  type="button"
                  className="mt-1 text-[11px] text-slate-400 underline hover:text-slate-700"
                  onClick={() => setSwitchingTo(release)}
                >
                  切到这一版
                </button>
              )}
            </li>
          ))}
        </ul>
        <QueryFailuresCard projectId={projectId} onGo={() => goTo('feedback')} />
        {/* 原生 window.confirm 会弹出带 "localhost:9222 显示" 的浏览器系统框，
            读起来像是站点出了问题；项目里本来就有这个组件。 */}
        <ConfirmationDialog
          open={Boolean(switchingTo)}
          danger
          title={`把线上切到第 ${switchingTo?.sequence} 版？`}
          description="线上问数会立即改用这一版的语义模型，当前线上版本进入历史。已发布的版本都是快照，之后随时可以再切回来。"
          confirmText="切换"
          loading={switchRelease.isPending}
          onConfirm={() => {
            if (switchingTo) switchRelease.mutate(switchingTo);
            setSwitchingTo(null);
          }}
          onClose={() => setSwitchingTo(null)}
        />
      </aside>
    </div>
  );
}

function CheckCell({ check, onGo }: { check: PublishCheck; onGo?: () => void }) {
  const body = (
    <>
      <div className="flex items-center gap-2">
        <span className={`h-3 w-3 shrink-0 rounded-full ${CHECK_DOT[check.state]}`} />
        <span className="text-xs font-medium text-slate-700">{check.label}</span>
        <span className={`ml-auto text-[11px] font-medium ${CHECK_TEXT[check.state]}`}>
          {check.status}
        </span>
      </div>
      <div className="mt-1 truncate text-[11px] text-slate-400">
        {onGo ? '点这里去运行' : check.hint}
      </div>
    </>
  );
  const className = `min-w-0 flex-1 rounded-md border p-2.5 text-left ${CHECK_BOX[check.state]}`;
  if (!onGo) return <div className={className}>{body}</div>;
  return (
    <button type="button" onClick={onGo} className={`${className} hover:border-amber-300`}>
      {body}
    </button>
  );
}

function QueueRow({
  item,
  readOnly,
  busy,
  onDecide,
}: {
  item: ReviewItem;
  readOnly: boolean;
  busy: boolean;
  onDecide: (confirm: boolean) => void;
}) {
  return (
    <li
      className={`flex items-start gap-2.5 border-b border-slate-100 px-3 py-2.5 last:border-b-0 ${
        item.tone === 'blocking' ? 'bg-red-50/40' : ''
      }`}
    >
      <span className="mt-0.5 shrink-0">
        <Badge tone={item.tone === 'blocking' ? 'red' : 'amber'}>{item.badge}</Badge>
      </span>
      <div className="min-w-0 flex-1 text-xs">
        <div className="text-slate-800">
          <b className="font-semibold">{item.title}</b>
          {item.detail && <span className="ml-2 text-slate-600">{item.detail}</span>}
        </div>
        {item.hint && <div className="mt-1 leading-relaxed text-slate-400">{item.hint}</div>}
      </div>
      {item.previewId && !readOnly && (
        <div className="flex shrink-0 items-center gap-3 pt-0.5">
          <button
            type="button"
            disabled={busy}
            className="text-[11px] font-medium text-blue-600 hover:text-blue-500 disabled:opacity-50"
            onClick={() => onDecide(true)}
          >
            {item.rejected ? '其实是对的' : '数值正确'}
          </button>
          {!item.rejected && (
            <button
              type="button"
              disabled={busy}
              className="text-[11px] font-medium text-red-600 hover:text-red-500 disabled:opacity-50"
              onClick={() => onDecide(false)}
            >
              不对
            </button>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * Ask the unpublished candidate directly. Same core path as the live query
 * except it binds to this revision, so a wrong answer here is a modeling
 * problem, not a release problem.
 */
function TrialQuestions({
  projectId,
  revision,
  trialQuestion,
  onTrialQuestionHandled,
}: Pick<WorkbenchContext, 'projectId' | 'revision'> & {
  trialQuestion?: string | null;
  onTrialQuestionHandled?: () => void;
}) {
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
  // 从反馈页带过来的那句话：自动跑一次，跑完由人确认「答对了」再存为用例。
  //
  // 必须挡住重复触发：试问是一次真实的模型调用，而 StrictMode 下 effect 会连跑
  // 两遍（挂载 → 清理 → 再挂载），父级清空 trialQuestion 的 setState 那时还没
  // 刷新，两次看到的都是同一句话。清空后把标记复位，同一句话再点一次仍然有效。
  const submitRef = useRef(submit);
  submitRef.current = submit;
  const handledRef = useRef<string | null>(null);
  useEffect(() => {
    if (!trialQuestion) {
      handledRef.current = null;
      return;
    }
    if (handledRef.current === trialQuestion) return;
    handledRef.current = trialQuestion;
    submitRef.current(trialQuestion);
    onTrialQuestionHandled?.();
  }, [trialQuestion, onTrialQuestionHandled]);
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
 * 右栏只留一句摘要，不再把「问数反馈」整页搬过来。
 *
 * 早先这里逐条列了 50 条没听懂的说法，一屏塞满、把发布历史挤没了，而第 4 步
 * 「问数反馈」正是同一份数据的正经去处（那里还能一键补进词典）。同一份证据在两个
 * 地方各展示一遍，用户不知道该在哪儿处理。
 */
function QueryFailuresCard({
  projectId,
  onGo,
}: {
  projectId: string;
  onGo: () => void;
}) {
  const failures = useQuery({
    queryKey: ['query-failures', projectId, 'without-liked'],
    // 发布页只看待处理的：已经处理掉的不该再提醒一遍。点赞也排掉——它是一条被人
    // 确认过的问答，不是发布前要清的缺口，算进来只会让这个数字越用越大。
    queryFn: () =>
      listQueryFailures(projectId, {
        limit: 50,
        status: 'open',
        excludeKinds: ['liked'],
      }),
  });
  const rows = feedbackRows(failures.data?.items ?? []);
  if (failures.isPending || rows.length === 0) return null;
  return (
    <div className="mt-5 rounded-lg border border-slate-200 p-3">
      {/*
        这句话原先三处都不实：①"没接住"——四种收场里 clarified 和 inferred 其实
        答上来了，只是靠反问和猜；②数字用的是 rows.length，而 rows 当时是前端把
        这一页 50 行聚合出来的，全量另算（实测界面报 25 种、真实 45 种）；③"最高
        频"——第一排序键是收场类型不是次数，rows[0] 常常只被问过一次。

        聚合下沉到 SQL 之后 total 就是真实种数了，这里可以照实说"多少种"。但排序
        仍是"收场类型优先"，所以例子还是只说例子。
      */}
      <div className="text-xs text-slate-700">
        有 <b className="font-semibold">{failures.data?.total ?? rows.length}</b> 条待处理的问数反馈
      </div>
      <div className="mt-1.5 text-[11px] leading-relaxed text-slate-400">
        比如「{rows[0].question}」。多数补个别名就能答上。
      </div>
      <button
        type="button"
        onClick={onGo}
        className="mt-2 text-[11px] font-medium text-blue-600 hover:text-blue-500"
      >
        去问数反馈处理 →
      </button>
    </div>
  );
}
