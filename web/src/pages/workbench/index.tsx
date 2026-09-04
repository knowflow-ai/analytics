import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Check, MessageSquareText } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from '@analytics/lib/router';
import { deriveCandidate, getModelingSummary, getRevision } from '@analytics/api/analytics';
import type { AnalyticsRevision } from '@analytics/api/types';
import { Badge, Button, Spinner, useToast } from '@analytics/components/ui';
import { describeError, REVISION_STATE_LABELS } from '@analytics/lib/labels';
import { AskFeedbackPanel } from './ask-feedback';
import type { FeedbackFixTarget } from './ask-feedback-state';
import { PublishPanel } from './publish';
import { CompletenessStrip } from './completeness-strip';
import { showsCompleteness } from './completeness';
import { useAvailableCanvasHeight } from '@analytics/lib/use-available-height';
import { TablesPanel } from './tables';
import { appPath } from '@analytics/api/edition';
import { normalizeWorkbenchStep, type WorkbenchStep } from './catalog-view';
import { SemanticCatalogPanel } from './semantic-catalog-panel';
import { ANALYTICS_FLUID_PANEL_CLASS } from '@analytics/lib/layout';

export type StepKey = WorkbenchStep;

export const WORKBENCH_STEPS: Array<{ key: StepKey; label: string }> = [
  { key: 'tables', label: '数据源' },
  { key: 'catalog', label: '语义建模' },
  { key: 'publish', label: '问数验证' },
  // 发布之后才有线上提问可看。它不是一次性建模步骤，而是回流入口：用户问了
  // 什么、系统听不懂什么，回到这里变成别名与术语。
  { key: 'feedback', label: '问数反馈' },
];

/** Everything a panel needs to read and advance the current revision. */
export interface WorkbenchContext {
  projectId: string;
  revision: AnalyticsRevision;
  /** Replace the revision after a successful write (every write returns it). */
  acceptRevision: (next: AnalyticsRevision) => void;
  /** Published/frozen revisions are immutable; panels disable writes. */
  readOnly: boolean;
  goTo: (step: StepKey) => void;
}

function Steps({
  active,
  done,
  onChange,
}: {
  active: StepKey;
  done: ReadonlySet<StepKey>;
  onChange: (key: StepKey) => void;
}) {
  return (
    <div className="flex items-center border-b border-slate-100 bg-slate-50 px-4">
      {WORKBENCH_STEPS.map((step, index) => {
        const isActive = step.key === active;
        const isDone = done.has(step.key) && !isActive;
        return (
          <div key={step.key} className="flex items-center">
            <button
              type="button"
              aria-current={isActive ? 'step' : undefined}
              onClick={() => onChange(step.key)}
              className={`flex items-center gap-2 py-2.5 pr-4 text-[13px] transition-colors ${
                isActive
                  ? 'font-semibold text-blue-600'
                  : isDone
                    ? 'text-slate-600 hover:text-slate-800'
                    : 'text-slate-400 hover:text-slate-600'
              }`}
            >
              <span
                className={`grid h-[19px] w-[19px] place-items-center rounded-full border-[1.5px] text-[10px] font-semibold ${
                  isActive
                    ? 'border-blue-600 bg-blue-600 text-white'
                    : isDone
                      ? 'border-green-600 bg-green-600 text-white'
                      : 'border-current'
                }`}
              >
                {isDone ? <Check className="h-3 w-3" /> : index + 1}
              </span>
              {step.label}
            </button>
            {index < WORKBENCH_STEPS.length - 1 && <span className="mr-3.5 text-slate-300">›</span>}
          </div>
        );
      })}
    </div>
  );
}

export function WorkbenchPage() {
  const { projectId = '' } = useParams();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const step = normalizeWorkbenchStep(params.get('step'));
  // 四个 tab 共用一个高度，见下面容器处的说明。measurementKey 用 step：切 tab 时
  // 上方的提示条可能出现或消失，顶边跟着变，不重测就会差一截。
  const goTo = useCallback(
    (next: StepKey) => setParams({ step: next }, { replace: true }),
    [setParams],
  );

  const summary = useQuery({
    queryKey: ['summary', projectId],
    queryFn: () => getModelingSummary(projectId),
  });
  const revisionId = summary.data?.revision_id ?? null;
  const revisionQuery = useQuery({
    queryKey: ['revision', projectId, revisionId],
    queryFn: () => getRevision(projectId, revisionId!),
    enabled: Boolean(revisionId),
  });
  const [revision, setRevision] = useState<AnalyticsRevision | null>(null);
  // 「问数反馈」里点「补进词典」时带过来的落点：跨步骤只传这一个值,由业务词典
  // 打开对应编辑器后立刻清空。存在这里而不是 URL 里,是因为它只在这一次跳转里
  // 有意义——刷新后重新打开一个空的术语弹窗只会让人困惑。
  const [dictionaryTarget, setDictionaryTarget] = useState<FeedbackFixTarget | null>(null);
  useEffect(() => {
    if (revisionQuery.data) setRevision(revisionQuery.data);
  }, [revisionQuery.data]);

  const acceptRevision = useCallback(
    (next: AnalyticsRevision) => {
      setRevision(next);
      queryClient.setQueryData(['revision', projectId, next.id], next);
      if (next.id !== revisionId) {
        queryClient.invalidateQueries({ queryKey: ['summary', projectId] });
      }
    },
    [projectId, queryClient, revisionId],
  );

  const derive = useMutation({
    mutationFn: () => deriveCandidate(projectId, revision!.id),
    onSuccess: (next) => {
      acceptRevision(next);
      toast.success('已基于发布版本创建新的草稿，可以继续编辑。');
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const done = useMemo(() => {
    const set = new Set<StepKey>();
    if (!revision) return set;
    const spec = revision.semantic_spec;
    if (spec.models.length) set.add('tables');
    if (spec.metrics.length || spec.dimensions.length) set.add('catalog');
    if (revision.state === 'published') set.add('publish');
    return set;
  }, [revision]);

  if (summary.isPending) return <Spinner />;
  if (summary.isError) {
    return <div className="text-sm text-red-600">{describeError(summary.error)}</div>;
  }

  const readOnly = Boolean(revision && revision.state !== 'draft' && revision.state !== 'validated');
  // 测量键要带上**任何会改变顶边位置的东西**：首屏进来时上方的版本信息和只读
  // 提示条还没渲染完，只按 step 测的话第一个 tab 会矮一截，切一次才对——用户
  // 实测就是这个。ResizeObserver 看的是祖先的尺寸，兄弟节点自己变高不会触发。
  const panelViewport = useAvailableCanvasHeight(
    true,
    `${step}|${revisionId ?? ''}|${readOnly}|${revisionQuery.isPending}`,
  );
  const context: WorkbenchContext | null = revision
    ? { projectId, revision, acceptRevision, readOnly, goTo }
    : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            to={appPath('/')}
            className="grid h-8 w-8 place-items-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-semibold text-slate-900">
                {summary.data.project_name}
              </h1>
              {revision && (
                <Badge tone={revision.state === 'published' ? 'green' : 'blue'}>
                  {REVISION_STATE_LABELS[revision.state]} · v{revision.etag}
                </Badge>
              )}
            </div>
            <div className="text-[11px] text-slate-400">
              {summary.data.active_release_id
                ? `线上版本 ${summary.data.active_release_id}`
                : '尚未发布'}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {readOnly && (
            <Button size="sm" loading={derive.isPending} onClick={() => derive.mutate()}>
              基于此版本继续编辑
            </Button>
          )}
          {summary.data.active_release_id && (
            <Link to={appPath(`/projects/${projectId}/ask`)}>
              <Button
                size="sm"
                variant="primary"
                icon={<MessageSquareText className="h-3.5 w-3.5" />}
              >
                开始问数
              </Button>
            </Link>
          )}
        </div>
      </div>

      {/*
        overflow-clip 而不是 overflow-hidden：两者都裁剪，但 overflow-hidden 在程序上
        **仍然是滚动容器**——任何 scrollIntoView（我们自己的、浏览器聚焦时的自动滚动、
        输入法的）都会去滚它，把整张卡的内容顶上去，而它没有滚动条，用户滚不回来，
        表现就是「页面拉不下去、下半截没了」。画布还会跟着错：它按
        getBoundingClientRect().top 算可用高度，卡被内部滚动后这个值变成负数。
        overflow-clip 只裁剪，不建立滚动端口，从根上没有这个状态。
      */}
      <div className="overflow-clip rounded-xl border border-slate-200 bg-white">
        <Steps active={step} done={done} onChange={goTo} />
        {revision && showsCompleteness(step) && (
          <CompletenessStrip revision={revision} goTo={goTo} />
        )}
        {readOnly && (
          <div className="border-b border-amber-100 bg-amber-50 px-4 py-2 text-xs text-amber-700">
            当前版本已{REVISION_STATE_LABELS[revision!.state]}，内容只读。
            点击右上角「基于此版本继续编辑」派生一个新草稿后再修改。
          </div>
        )}
        {/*
          四个 tab 共用一个高度：不固定的话容器跟着各自内容撑开，切 tab 时整块面板
          忽上忽下。高度用关系画布同一个公式（顶边到视口底），所以画布那页和其它三页
          的框对得齐。

          用 min-height + grid 拉伸，**不设 overflow**：上面那条注释说得很清楚，这张
          卡里不能再出现滚动容器。内容比这个高度长时照常把页面撑长，和以前一样。
          grid-template-rows:1fr 让唯一的子元素铺满这个高度——面板内部的分割线要靠它
          才画得到底，否则只画到内容末尾就断了。
        */}
        <div
          ref={panelViewport.ref}
          className={`${ANALYTICS_FLUID_PANEL_CLASS} grid [grid-template-rows:1fr]`}
          style={
            panelViewport.height ? { minHeight: panelViewport.height } : undefined
          }
        >
          {!context && (revisionId && revisionQuery.isPending) && <Spinner />}
          {!context && !revisionId && (
            <TablesPanel projectId={projectId} revision={null} acceptRevision={acceptRevision} />
          )}
          {context && step === 'tables' && (
            <TablesPanel
              projectId={projectId}
              revision={context.revision}
              acceptRevision={acceptRevision}
              readOnly={readOnly}
            />
          )}
          {context && step === 'catalog' && (
            <SemanticCatalogPanel
              key={context.revision.id}
              {...context}
              dictionaryTarget={dictionaryTarget}
              onDictionaryTargetHandled={() => setDictionaryTarget(null)}
            />
          )}
          {context && step === 'publish' && <PublishPanel {...context} />}
          {context && step === 'feedback' && (
            <AskFeedbackPanel
              {...context}
              onFixInDictionary={(target) => {
                setDictionaryTarget(target);
                goTo('catalog');
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
