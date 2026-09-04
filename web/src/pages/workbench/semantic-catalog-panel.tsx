import { BookOpenText, GitBranch, ListTree, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Badge, Button, Empty } from '@analytics/components/ui';
import type { AnalyticsSemanticContextEntry } from '@analytics/api/types';
import type { FeedbackFixTarget } from './ask-feedback-state';
import type { WorkbenchContext } from './index';
import {
  AiModelingPanel,
  hasReviewableModelingProposal,
  isCurrentModelingJob,
  useModelingJobSession,
} from './ai-modeling';
import {
  buildCatalogInventory,
  buildScopeDiagnostics,
  serializeCatalogResource,
  sliceCatalogRows,
  type CatalogInventory,
  type CatalogResourceKind,
  visibleSemanticContext,
} from './catalog-view';
import { CanvasPanel } from './canvas';
import {
  BusinessDictionaryPanel,
  type BusinessDictionarySection,
} from './business-dictionary';

export type CatalogView = 'overview' | 'graph' | 'dictionary' | 'ai' | 'scopes';

export const CATALOG_NAV_ITEMS: ReadonlyArray<{
  key: Extract<CatalogView, 'overview' | 'graph' | 'dictionary'>;
  label: string;
}> = [
  { key: 'graph', label: '实体与关系' },
  { key: 'dictionary', label: '业务词典' },
  { key: 'overview', label: '目录概览' },
];

export const CATALOG_AI_ACTION = {
  label: 'AI 一键建模',
  variant: 'primary',
} as const;

export function catalogAiActionState({
  view,
  modelingRunning,
  modelingReady,
}: {
  view: CatalogView;
  modelingRunning: boolean;
  modelingReady: boolean;
}): {
  label: string;
  disabled: boolean;
  current: boolean;
} {
  const current = view === 'ai';
  const label = modelingRunning
    ? 'AI 建模中'
    : modelingReady
      ? current
        ? '正在审核'
        : '审核 AI 建议'
      : CATALOG_AI_ACTION.label;

  return {
    label,
    disabled: current,
    current,
  };
}

/**
 * 进入「语义建模」时的落点。
 *
 * 目录非空时落在实体与关系画布,不是目录概览:概览是一份读不动的资源清单
 * （Model / Field / Metric 的计数与 DTO），用户第一眼看到它并不知道该动哪里；
 * 这一步真正要人确认的是关系基数与字段角色，那些都在画布上。目录还是空的时候
 * 仍落到 AI 一键建模——那时画布上没有东西可看。
 */
export function initialCatalogView(spec: {
  metrics: ReadonlyArray<unknown>;
  dimensions: ReadonlyArray<unknown>;
}): CatalogView {
  return spec.metrics.length || spec.dimensions.length ? 'graph' : 'ai';
}

export function catalogResourceDestination(kind: CatalogResourceKind):
  | { view: 'overview' }
  | { view: 'dictionary'; section: BusinessDictionarySection } {
  if (kind === 'terms') return { view: 'dictionary', section: 'terms' };
  if (kind === 'dimensionValues') {
    return { view: 'dictionary', section: 'dimensionValues' };
  }
  return { view: 'overview' };
}

const CONTEXT_TARGET_LABELS: Record<AnalyticsSemanticContextEntry['target_type'], string> = {
  project: '项目',
  model: '实体',
  metric: '指标',
  dimension: '维度',
  query_scope: '查询作用域',
};

const CONTEXT_KIND_LABELS: Record<AnalyticsSemanticContextEntry['kind'], string> = {
  definition: '定义',
  convention: '约定',
  scope: '范围',
  exception: '例外',
  time_policy: '时间口径',
};

const CONTEXT_SOURCE_LABELS: Record<AnalyticsSemanticContextEntry['source_type'], string> = {
  database_comment: '数据库注释',
  profile_evidence: '数据剖析',
  knowledge_document: '知识文档',
  human_convention: '人工约定',
  catalog_description: '目录说明',
};

export function semanticContextSourceLabel(
  source: AnalyticsSemanticContextEntry['source_type'],
): string {
  return CONTEXT_SOURCE_LABELS[source];
}

const RESOURCE_LABELS: Record<CatalogResourceKind, string> = {
  models: 'Model / 实体',
  fields: 'Field / 字段',
  dimensions: 'Dimension / 维度',
  measures: 'Measure / 度量',
  metrics: 'Metric / 指标',
  terms: 'Term / 术语',
  dimensionValues: 'DimensionValue / 维度值',
};

const RESOURCE_ORDER = Object.keys(RESOURCE_LABELS) as CatalogResourceKind[];

/**
 * The public modeling surface. QueryScope remains in the wire contract for
 * query compatibility, but is exposed only as a deterministic diagnostic.
 */
export function SemanticCatalogPanel(
  props: WorkbenchContext & {
    /** 从「问数反馈」跳过来时要打开的词典成员;打开后由父级清空。 */
    dictionaryTarget?: FeedbackFixTarget | null;
    onDictionaryTargetHandled?: () => void;
  },
) {
  const { dictionaryTarget = null, onDictionaryTargetHandled, ...context } = props;
  const spec = context.revision.semantic_spec;
  const modelingSession = useModelingJobSession(
    context.projectId,
    context.revision.id,
  );
  const [view, setView] = useState<CatalogView>(() => initialCatalogView(spec));
  const [dictionarySection, setDictionarySection] =
    useState<BusinessDictionarySection>('terms');
  // 从问数反馈带着落点跳过来:先把视图切到业务词典的对应分区,再由词典面板打开
  // 编辑器。少了这一步,用户会落在实体与关系画布上,还得自己找到业务词典。
  useEffect(() => {
    if (!dictionaryTarget) return;
    setDictionarySection(
      dictionaryTarget.kind === 'dimensionValue' ? 'dimensionValues' : 'terms',
    );
    setView('dictionary');
  }, [dictionaryTarget]);
  const navIcons: Record<(typeof CATALOG_NAV_ITEMS)[number]['key'], ReactNode> = {
    overview: <BookOpenText className="h-3.5 w-3.5" />,
    graph: <GitBranch className="h-3.5 w-3.5" />,
    dictionary: <BookOpenText className="h-3.5 w-3.5" />,
  };
  const openDictionary = (section: BusinessDictionarySection) => {
    setDictionarySection(section);
    setView('dictionary');
  };
  const modelingStatus = modelingSession.job.data?.status;
  const modelingJobIsCurrent = isCurrentModelingJob(
    modelingSession.job.data,
    context.revision.etag,
  );
  const modelingRunning =
    modelingJobIsCurrent &&
    (modelingStatus === 'queued' || modelingStatus === 'running');
  const modelingReady = hasReviewableModelingProposal(
    modelingSession.job.data,
    modelingSession.proposal.data,
    context.revision.etag,
  );
  const aiAction = catalogAiActionState({
    view,
    modelingRunning,
    modelingReady,
  });
  const nestedContext: WorkbenchContext = {
    ...context,
    goTo: (step) => {
      // 采用 AI 建议后回到语义建模,落点与首次进入一致:能动手的画布,不是概览。
      if (step === 'catalog') setView('graph');
      context.goTo(step);
    },
  };

  return (
    <div>
      <div className="flex items-center gap-1 border-b border-slate-100 px-4 py-2">
        {CATALOG_NAV_ITEMS.map((item) => (
          <Button
            key={item.key}
            size="sm"
            variant={view === item.key ? 'primary' : 'ghost'}
            icon={navIcons[item.key]}
            onClick={() => setView(item.key)}
          >
            {item.label}
          </Button>
        ))}
        <span className="ml-auto hidden text-[11px] text-slate-400 lg:inline">
          对外目录覆盖全部 {spec.metrics.length} 个指标与 {spec.dimensions.length} 个维度
        </span>
        <Button
          size="sm"
          variant={CATALOG_AI_ACTION.variant}
          icon={<Sparkles className="h-3.5 w-3.5" />}
          disabled={aiAction.disabled}
          aria-current={aiAction.current ? 'page' : undefined}
          onClick={() => setView('ai')}
        >
          {aiAction.label}
        </Button>
        <Button
          size="sm"
          variant={view === 'scopes' ? 'primary' : 'ghost'}
          icon={<ListTree className="h-3.5 w-3.5" />}
          onClick={() => setView('scopes')}
        >
          高级诊断
        </Button>
      </div>
      {view !== 'ai' && modelingRunning && (
        <button
          type="button"
          className="flex w-full items-center gap-3 border-b border-blue-100 bg-blue-50/70 px-4 py-2 text-left text-xs text-blue-800"
          onClick={() => setView('ai')}
        >
          <Sparkles className="h-4 w-4 shrink-0" />
          <span>
            AI 建模正在后台进行，可以继续核对目录；完成后将在这里提醒审核。
          </span>
          <span className="ml-auto font-medium">查看进度</span>
        </button>
      )}
      {view !== 'ai' && modelingReady && (
        <button
          type="button"
          className="flex w-full items-center gap-3 border-b border-emerald-100 bg-emerald-50/70 px-4 py-2 text-left text-xs text-emerald-800"
          onClick={() => setView('ai')}
        >
          <Sparkles className="h-4 w-4 shrink-0" />
          <span>AI 建模建议已生成，采用前需要逐项审核。</span>
          <span className="ml-auto font-medium">去审核</span>
        </button>
      )}
      {view === 'overview' && (
        <CatalogOverview {...nestedContext} onOpenDictionary={openDictionary} />
      )}
      {view === 'graph' && <CanvasPanel key={context.revision.id} {...nestedContext} />}
      {view === 'dictionary' && (
        <BusinessDictionaryPanel
          context={nestedContext}
          section={dictionarySection}
          onSectionChange={setDictionarySection}
          onOpenGraph={() => setView('graph')}
          openTarget={dictionaryTarget}
          onOpenTargetHandled={onDictionaryTargetHandled}
        />
      )}
      {view === 'ai' && (
        <AiModelingPanel
          key={context.revision.id}
          {...nestedContext}
          modelingSession={modelingSession}
        />
      )}
      {view === 'scopes' && <QueryScopeDiagnostics {...nestedContext} />}
    </div>
  );
}

function CatalogOverview({
  revision,
  onOpenDictionary,
}: WorkbenchContext & {
  onOpenDictionary: (section: BusinessDictionarySection) => void;
}) {
  const spec = revision.semantic_spec;
  const inventory = useMemo(
    () => buildCatalogInventory(spec, revision.semantic_catalog),
    [revision.semantic_catalog, spec],
  );
  const contexts = useMemo(() => visibleSemanticContext(spec), [spec]);
  const targetNames = useMemo(() => {
    const values: Array<[string, string]> = [[revision.project_id, '当前项目']];
    spec.models.forEach((item) => values.push([item.id, item.name]));
    spec.metrics.forEach((item) => values.push([item.id, item.name]));
    spec.dimensions.forEach((item) => values.push([item.id, item.name]));
    spec.datasets.forEach((item) => values.push([item.id, item.name]));
    return new Map(values);
  }, [revision.project_id, spec]);

  if (!inventory.models.length) {
    return <Empty title="语义目录还是空的" hint="先从数据源导入表，再运行 AI 建模或手工维护实体。" />;
  }

  return (
    <div className="flex flex-col gap-6 px-6 py-5">
      <section>
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">完整语义目录</h2>
            <p className="mt-0.5 text-xs text-slate-400">
              所有已治理实体、指标、维度、术语和上下文都会对问数可发现；系统不会要求用户先维护一个“分析主题”。
            </p>
          </div>
          <div className="flex shrink-0 gap-1.5">
            <Badge tone="blue">{inventory.models.length} Model</Badge>
            <Badge tone="sky">{inventory.fields.length} Field</Badge>
            <Badge tone="slate">{inventory.measures.length} Measure</Badge>
            <Badge tone="green">{inventory.metrics.length} Metric</Badge>
            <Badge tone="violet">{inventory.dimensions.length} Dimension</Badge>
          </div>
        </div>
        <CatalogInventoryBrowser
          inventory={inventory}
          onOpenDictionary={onOpenDictionary}
        />
      </section>

      <SemanticContextList entries={contexts} targetNames={targetNames} />
    </div>
  );
}

function CatalogInventoryBrowser({
  inventory,
  onOpenDictionary,
}: {
  inventory: CatalogInventory;
  onOpenDictionary: (section: BusinessDictionarySection) => void;
}) {
  const [kind, setKind] = useState<CatalogResourceKind>('models');
  const [limit, setLimit] = useState(100);
  const rows = inventory[kind];
  const page = sliceCatalogRows(rows, limit);
  const selectKind = (next: CatalogResourceKind) => {
    const destination = catalogResourceDestination(next);
    if (destination.view === 'dictionary') {
      onOpenDictionary(destination.section);
      return;
    }
    setKind(next);
    setLimit(100);
  };
  return (
    <div>
      <div className="mb-3 grid gap-2 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
        {RESOURCE_ORDER.map((resourceKind) => (
          <button
            key={resourceKind}
            type="button"
            onClick={() => selectKind(resourceKind)}
            className={`rounded-lg border px-3 py-2 text-left transition-colors ${
              kind === resourceKind
                ? 'border-blue-300 bg-blue-50 text-blue-800'
                : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
            }`}
          >
            <div className="text-[11px] font-medium">{RESOURCE_LABELS[resourceKind]}</div>
            <div className="mt-0.5 text-lg font-semibold">{inventory[resourceKind].length}</div>
          </button>
        ))}
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-4 py-8 text-center text-xs text-slate-400">
          当前目录没有 {RESOURCE_LABELS[kind]} 资源。
        </div>
      ) : (
        <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {page.visible.map((row) => (
            <li key={row.id} className="px-3 py-2.5 text-xs">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-medium text-slate-800">{row.title}</div>
                  <div className="truncate text-[11px] text-slate-400">{row.subtitle}</div>
                </div>
                <span className="max-w-[40%] truncate font-mono text-[10px] text-slate-300" title={row.id}>
                  {row.id}
                </span>
              </div>
              {row.description && <div className="mt-1 whitespace-pre-wrap leading-relaxed text-slate-500">{row.description}</div>}
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
                {row.details.map((item) => (
                  <span key={`${item.label}:${item.value}`}><b className="font-medium text-slate-400">{item.label}</b> {item.value}</span>
                ))}
              </div>
              {row.aliases.length > 0 && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1">
                  <span className="text-[10px] text-slate-400">别名</span>
                  {row.aliases.map((alias, index) => <Badge key={`${alias}:${index}`}>{alias}</Badge>)}
                </div>
              )}
              <details className="mt-2 rounded border border-slate-100 bg-slate-50/60 text-[11px]">
                <summary className="cursor-pointer px-2 py-1 text-slate-500 hover:text-slate-700">
                  查看完整 DTO（无字段省略）
                </summary>
                <pre className="max-h-80 overflow-auto border-t border-slate-100 p-2 font-mono leading-relaxed text-slate-600">
                  {serializeCatalogResource(row)}
                </pre>
              </details>
            </li>
          ))}
        </ul>
      )}
      {page.remaining > 0 && (
        <div className="mt-3 flex items-center justify-center gap-3 text-xs text-slate-400">
          已显示 {page.visible.length}/{rows.length}
          <Button size="sm" onClick={() => setLimit(page.nextLimit)}>
            再显示 {Math.min(100, page.remaining)} 个
          </Button>
        </div>
      )}
    </div>
  );
}

function SemanticContextList({
  entries,
  targetNames,
}: {
  entries: AnalyticsSemanticContextEntry[];
  targetNames: ReadonlyMap<string, string>;
}) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        语义上下文 {entries.length} · 只读
      </h3>
      {entries.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-xs text-slate-400">
          当前目录没有已审核的语义上下文。
        </div>
      ) : (
        <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {entries.map((entry) => (
            <li key={entry.id} className="px-3 py-2.5 text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="blue">{CONTEXT_TARGET_LABELS[entry.target_type]}</Badge>
                <Badge tone="violet">{CONTEXT_KIND_LABELS[entry.kind]}</Badge>
                <Badge tone="slate">{semanticContextSourceLabel(entry.source_type)}</Badge>
                <span className="font-medium text-slate-700">
                  {targetNames.get(entry.target_id) ?? entry.target_id}
                </span>
              </div>
              <div className="mt-1.5 whitespace-pre-wrap leading-relaxed text-slate-600">{entry.text}</div>
              {entry.source_ref && <div className="mt-1 break-all font-mono text-[10px] text-slate-400">{entry.source_ref}</div>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function QueryScopeDiagnostics({ revision }: WorkbenchContext) {
  const diagnostics = useMemo(() => buildScopeDiagnostics(revision.semantic_spec), [revision.semantic_spec]);
  if (!diagnostics.length) {
    return (
      <Empty
        title="当前版本尚无已编译查询作用域"
        hint="完整语义目录仍然有效；运行 AI 建模后，服务端会确定性生成兼容当前查询接口的只读作用域。"
      />
    );
  }
  return (
    <div className="px-6 py-5">
      <div className="mb-4 rounded-lg border border-blue-100 bg-blue-50/60 px-3 py-2 text-xs leading-relaxed text-blue-800">
        查询作用域由完整语义目录确定性编译，仅用于固定事实根、精确 Join 路径与 COUNT 绑定。请在实体、关系、指标、维度或术语中修改业务语义；这里没有创建、编辑或删除入口。
      </div>
      <ul className="flex flex-col gap-3">
        {diagnostics.map((scope) => (
          <li key={scope.id} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-[13px] font-semibold text-slate-900">{scope.name}</div>
              <Badge tone={scope.hasRoute ? 'green' : 'amber'}>
                {scope.hasRoute ? '已编译' : '兼容路由缺失'}
              </Badge>
              <span className="ml-auto font-mono text-[10px] text-slate-400">{scope.id}</span>
            </div>
            <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
              <ScopeValue label="事实根" values={[scope.rootName]} />
              <ScopeValue
                label="默认计数"
                values={scope.defaultCountName ? [scope.defaultCountName] : ['未配置（直接 COUNT(*) 将拒绝）']}
              />
              <ScopeValue label="成员实体" values={scope.modelNames} />
              <ScopeValue label="指标" values={scope.metricNames} empty="无" />
              <ScopeValue label="维度" values={scope.dimensionNames} empty="无" />
              <ScopeValue label="精确路径" values={scope.pathLabels} empty="仅事实根，无跨实体路径" />
            </dl>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ScopeValue({ label, values, empty = '无' }: { label: string; values: string[]; empty?: string }) {
  return (
    <div>
      <dt className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="mt-1 flex flex-wrap gap-1 text-slate-600">
        {values.length ? values.map((value, index) => (
          <span key={`${value}:${index}`} className="rounded bg-slate-50 px-1.5 py-0.5">{value}</span>
        )) : <span className="text-slate-400">{empty}</span>}
      </dd>
    </div>
  );
}
