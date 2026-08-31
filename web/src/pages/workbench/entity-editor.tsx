import { useMutation } from '@tanstack/react-query';
import { Clock, CornerDownRight, Lock } from 'lucide-react';
import { Fragment, useMemo, useRef, useState, type ReactNode } from 'react';
import { deleteCatalogResource, newResourceId, previewCatalogDeletion, saveDimension, saveHierarchy, saveMetric, saveModel, versionOf } from '@analytics/api/analytics';
import { MetricEditor, applyMetricEditorValues } from './metric-editor';
import type { MetricDefinitionSources } from './metric-definition';
import { AliasSuggestButton } from './alias-suggest-button';
import { DimensionDictionarySection } from './dimension-dictionary-section';
import { DimensionEditor } from './dimension-editor';
import {
  HierarchyEditor,
  applyHierarchyValues,
  hierarchyInitial,
  type HierarchyEditorValues,
} from './hierarchy-editor';
import { applyDimensionEditorValues } from './dimension-definition';
import type {
  AnalyticsCatalogDimension,
  AnalyticsCatalogHierarchy,
  AnalyticsCatalogMetric,
  AnalyticsField,
  AnalyticsRevision,
} from '@analytics/api/types';
import {
  Badge,
  Button,
  ConfirmationDialog,
  Dialog,
  Field,
  Input,
  Select,
  cx,
  useToast,
} from '@analytics/components/ui';
import { updateCatalogModelBasic, updateCatalogModelFieldRole, type CatalogFieldRoleInput } from '@analytics/lib/catalog-model-editor';
import { describeError } from '@analytics/lib/labels';
import { fieldRoleVisual } from '@analytics/lib/field-role';
import { derivedFromField, resolveEntityDetail, type EntitySelection } from './entity-detail';
import {
  DeletionPreviewGuard,
  deletionResourceKey,
  describeDeletionEffects,
  relationReferences,
  roleChangeBlockers,
} from './field-lock';
import { ContextualTermButton } from './business-dictionary';

type Kind = CatalogFieldRoleInput['kind'];
type DeletionKind = 'dimensions' | 'metrics' | 'models' | 'hierarchies';
const DELETION_RESOURCE_KIND: Record<DeletionKind, string> = {
  dimensions: 'dimension',
  metrics: 'metric',
  models: 'model',
  hierarchies: 'hierarchy',
};
interface DeletionTarget {
  kind: DeletionKind;
  id: string;
  label: string;
  name: string;
}
interface PendingDeletion extends DeletionTarget {
  impactHash: string;
  lines: string[];
}

const KIND_OPTIONS: Array<{ value: Kind; label: string }> = [
  { value: 'identifier', label: '标识' },
  { value: 'dimension', label: '维度' },
  { value: 'time', label: '时间' },
  { value: 'measure', label: '度量' },
  { value: 'field', label: '普通字段' },
];
const AGG_OPTIONS = ['sum', 'count', 'count_distinct', 'avg', 'min', 'max'] as const;
const splitList = (text: string) => text.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);
const joinAlias = (items: string[]) => items.join(',') || null;

interface Props {
  projectId: string;
  revision: AnalyticsRevision;
  modelId: string;
  readOnly: boolean;
  acceptRevision: (next: AnalyticsRevision) => void;
  onClose: () => void;
}

/**
 * Everything about one business entity in a single dialog: its naming, each
 * field's governed role, and the dimensions/metrics it owns. All writes go
 * through the full catalog DTOs so nothing is lost on round-trip.
 */
export function EntityEditor({ projectId, revision, modelId, readOnly, acceptRevision, onClose }: Props) {
  const toast = useToast();
  const spec = revision.semantic_spec;
  const catalog = revision.semantic_catalog;
  const model = spec.models.find((m) => m.id === modelId);
  const catalogModel = catalog.models.find((m) => m.id === modelId);
  const fields = spec.fields.filter((f) => f.model_id === modelId);
  const dimensions = spec.dimensions.filter((d) => d.model_id === modelId);
  const metrics = spec.metrics.filter((m) => m.model_id === modelId);
  const modelHierarchies = (catalog.hierarchies ?? []).filter((h) => h.modelId === modelId);
  const dimensionNameById = useMemo(
    () => new Map(spec.dimensions.map((d) => [d.id, d.name])),
    [spec.dimensions],
  );
  const modelNameById = useMemo(() => new Map(spec.models.map((m) => [m.id, m.name])), [spec.models]);
  // 口径表达式可引用的来源:本模型的受治理度量与物理列,以及可被组合的其它指标。
  const metricSources = useMemo<MetricDefinitionSources>(
    () => ({
      measures: catalogModel?.modelDetail.measures ?? [],
      fieldColumns: spec.fields.filter((f) => f.model_id === modelId).map((f) => f.column),
      metrics: catalog.metrics.map((m) => ({ id: m.id, bizName: m.bizName, name: m.name })),
    }),
    [catalogModel, catalog.metrics, spec.fields, modelId],
  );
  // 角色锁跟服务端走:只有 MEASURE 型指标口径引用的度量字段,改角色会被编译拒绝。
  // 其它引用(关系/自派生维度指标/主题/层级)都不锁——纠错 AI 的角色是主工作流。
  const blockersByField = useMemo(() => {
    const map = new Map<string, ReturnType<typeof roleChangeBlockers>>();
    fields.forEach((field) => {
      const blockers = roleChangeBlockers(field, catalog.metrics);
      if (blockers.length > 0) map.set(field.id, blockers);
    });
    return map;
  }, [catalog.metrics, fields]);

  const [basic, setBasic] = useState(() => ({
    name: model?.name ?? '',
    bizName: catalogModel?.bizName ?? model?.biz_name ?? '',
    description: model?.description ?? '',
    aliases: (model?.aliases ?? []).join('，'),
  }));
  // 右栏永远是编辑器,总览不再作为独立表面存在:左侧一张导航树(字段+其
  // 派生物+层级),右侧只有一个职责。进入即选中第一个字段;只读模式不自动
  // 选中,右侧显示一览提示。
  const [selection, setSelection] = useState<EntitySelection>(() =>
    !readOnly && fields[0] ? { kind: 'field', id: fields[0].id } : null,
  );
  // 「取消」= 放弃本次修改留在原地:epoch 进编辑器 key,强制重挂载回到已保存值。
  const [editEpoch, setEditEpoch] = useState(0);
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(null);
  const deletionPreviewGuard = useRef(new DeletionPreviewGuard());
  const cancelEdit = () => setEditEpoch((n) => n + 1);
  const cancelDeletionReview = () => {
    deletionPreviewGuard.current.cancel();
    setPendingDeletion(null);
  };
  const closeEditor = () => {
    cancelDeletionReview();
    onClose();
  };
  const fallbackSelection = () =>
    setSelection(fields[0] ? { kind: 'field', id: fields[0].id } : null);

  const version = versionOf(revision);
  const onSaved = (label: string) => (next: AnalyticsRevision) => {
    acceptRevision(next);
    toast.success(`${label}已保存。`);
  };
  const saveBasic = useMutation({
    mutationFn: () =>
      saveModel(
        projectId,
        revision.id,
        version,
        updateCatalogModelBasic(catalogModel!, {
          name: basic.name.trim(),
          bizName: basic.bizName.trim() || catalogModel!.bizName,
          description: basic.description,
          alias: joinAlias(splitList(basic.aliases)),
          filterSql: catalogModel!.filterSql ?? null,
        }),
      ),
    onSuccess: onSaved('实体'),
    onError: (error) => toast.error(describeError(error)),
  });
  const saveField = useMutation({
    mutationFn: ({ field, input }: { field: AnalyticsField; input: CatalogFieldRoleInput }) =>
      saveModel(projectId, revision.id, version, updateCatalogModelFieldRole(catalogModel!, field.column, input)),
    onSuccess: onSaved('字段'),
    onError: (error) => toast.error(describeError(error)),
  });
  const saveDim = useMutation({
    mutationFn: (dimension: AnalyticsCatalogDimension) => saveDimension(projectId, revision.id, version, dimension),
    onSuccess: onSaved('维度'),
    onError: (error) => toast.error(describeError(error)),
  });
  const saveMet = useMutation({
    mutationFn: (metric: AnalyticsCatalogMetric) => saveMetric(projectId, revision.id, version, metric),
    onSuccess: onSaved('指标'),
    onError: (error) => toast.error(describeError(error)),
  });
  const saveHier = useMutation({
    mutationFn: (hierarchy: AnalyticsCatalogHierarchy) =>
      saveHierarchy(projectId, revision.id, version, hierarchy),
    onSuccess: onSaved('层级'),
    onError: (error) => toast.error(describeError(error)),
  });
  const previewRemove = useMutation({
    mutationFn: async (target: DeletionTarget): Promise<PendingDeletion> => {
      // 服务端 planner 会级联(主题 unlink、依赖指标、维度值),先把清单亮出来。
      const impact = await previewCatalogDeletion(projectId, revision.id, version, target.kind, target.id);
      const names = new Map<string, string>();
      catalog.dimensions.forEach((d) => names.set(deletionResourceKey('dimension', d.id), d.name));
      catalog.metrics.forEach((m) => names.set(deletionResourceKey('metric', m.id), m.name));
      (catalog.hierarchies ?? []).forEach((h) =>
        names.set(deletionResourceKey('hierarchy', h.id), h.name),
      );
      catalog.dataSets?.forEach((d) => names.set(deletionResourceKey('dataset', d.id), d.name));
      spec.datasets.forEach((d) => names.set(deletionResourceKey('dataset', d.id), d.name));
      spec.models.forEach((m) => names.set(deletionResourceKey('model', m.id), m.name));
      spec.relations.forEach((r) =>
        names.set(
          deletionResourceKey('relation', r.id),
          `${names.get(deletionResourceKey('model', r.left_model_id)) ?? ''} — ${names.get(deletionResourceKey('model', r.right_model_id)) ?? ''}`,
        ),
      );
      return {
        ...target,
        impactHash: impact.impact_hash,
        lines: describeDeletionEffects(
          impact.effects,
          {
            resource_kind: DELETION_RESOURCE_KIND[target.kind],
            resource_id: target.id,
          },
          names,
        ),
      };
    },
  });
  const requestDeletion = async (target: DeletionTarget) => {
    const generation = deletionPreviewGuard.current.begin();
    if (generation === null) return;
    try {
      const pending = await previewRemove.mutateAsync(target);
      if (deletionPreviewGuard.current.settle(generation)) {
        setPendingDeletion(pending);
      }
    } catch (error) {
      if (deletionPreviewGuard.current.settle(generation)) {
        toast.error(describeError(error));
      }
    }
  };
  const remove = useMutation({
    mutationFn: (pending: PendingDeletion) =>
      deleteCatalogResource(
        projectId,
        revision.id,
        version,
        pending.kind,
        pending.id,
        pending.impactHash,
      ),
    onSuccess: (next, pending) => {
      setPendingDeletion(null);
      acceptRevision(next);
      fallbackSelection();
      if (pending.kind === 'models') onClose();
      toast.success('已删除。');
    },
    onError: (error) => {
      setPendingDeletion(null);
      toast.error(describeError(error));
    },
  });

  if (!model || !catalogModel) return null;

  const detail = resolveEntityDetail(selection, {
    fields,
    dimensions: catalog.dimensions,
    metrics: catalog.metrics,
    hierarchies: modelHierarchies,
  });
  const isSelected = (kind: string, id: string) =>
    selection !== null && 'id' in selection && selection.kind === kind && selection.id === id;

  const listRow = (
    key: string,
    selected: boolean,
    onPick: () => void,
    content: ReactNode,
  ) => (
    <li key={key}>
      <button
        type="button"
        disabled={readOnly || deletionBusy}
        onClick={onPick}
        className={cx(
          'flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left transition-colors',
          !readOnly && 'hover:bg-white',
          selected && 'bg-white ring-1 ring-inset ring-sky-300',
        )}
      >
        {content}
      </button>
    </li>
  );

  // 不挂在本模型任何字段上的派生物(公式指标/表达式维度),树里单独成组。
  const fieldIdSet = new Set(fields.map((f) => f.id));
  const detachedDimensions = dimensions.filter((d) => !d.field_id || !fieldIdSet.has(d.field_id));
  const detachedMetrics = metrics.filter((m) => !m.field_id || !fieldIdSet.has(m.field_id));

  const derivedRow = (
    kind: 'dimension' | 'metric',
    item: { id: string; name: string; aliases: string[] },
    extra: string | null,
    indent: boolean,
  ) => (
    <tr
      key={`${kind}:${item.id}`}
      onClick={readOnly || deletionBusy ? undefined : () => setSelection({ kind, id: item.id })}
      className={cx(
        'transition-colors',
        !readOnly && !deletionBusy && 'cursor-pointer hover:bg-slate-50',
        isSelected(kind, item.id) && 'bg-sky-50',
      )}
    >
      <td colSpan={3} className={cx('py-1 pr-3', indent ? 'pl-5' : 'pl-2')}>
        <span className="inline-flex min-w-0 max-w-full items-center gap-1.5">
          {indent && <CornerDownRight className="h-3 w-3 shrink-0 text-slate-300" />}
          <Badge tone={kind === 'dimension' ? 'sky' : 'green'} variant="outline">
            {kind === 'dimension' ? '维度' : '指标'}
          </Badge>
          <span className="truncate text-slate-700">{item.name}</span>
          {extra && <span className="shrink-0 text-[11px] text-slate-400">{extra}</span>}
          {item.aliases.length > 0 && (
            <span className="truncate text-[11px] text-slate-400">{item.aliases.join('、')}</span>
          )}
        </span>
      </td>
    </tr>
  );

  const detailTitle =
    detail.kind === 'field'
      ? `字段 · ${detail.field.column}`
      : detail.kind === 'dimension'
        ? `维度 · ${detail.dimension.name}`
        : detail.kind === 'metric'
          ? `指标 · ${detail.metric.name}`
          : detail.kind === 'hierarchy'
            ? detail.hierarchy
              ? `层级 · ${detail.hierarchy.name}`
              : '新建层级'
            : '已失效';

  const deletionBusy = previewRemove.isPending || remove.isPending;

  return (
    <>
      <Dialog
        open
        title={model.name}
        onClose={pendingDeletion ? () => undefined : closeEditor}
        inactive={pendingDeletion !== null}
        width="max-w-[min(94vw,1320px)]"
        height="h-[88vh]"
        footer={
          <>
            {!readOnly && (
              <Button
                variant="danger"
                className="mr-auto"
                loading={deletionBusy}
                onClick={() =>
                  void requestDeletion({
                    kind: 'models',
                    id: model.id,
                    label: '实体',
                    name: model.name,
                  })
                }
              >
                删除实体
              </Button>
            )}
            <Button onClick={closeEditor}>关闭</Button>
          </>
        }
      >
      <div className="flex h-full min-h-0 flex-col gap-4 text-xs">
        <section className="shrink-0">
          <div className="mb-2 flex items-center justify-between">
            <div className="font-semibold text-slate-700">基本信息</div>
            <span className="font-mono text-slate-400">{[model.schema_name, model.table].filter(Boolean).join('.')}</span>
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
            <Field label="业务名称">
              <Input value={basic.name} onChange={(e) => setBasic({ ...basic, name: e.target.value })} />
            </Field>
            <Field label="英文标识">
              <Input value={basic.bizName} onChange={(e) => setBasic({ ...basic, bizName: e.target.value })} />
            </Field>
            <Field label="别名" hint="用「，」分隔">
              <Input value={basic.aliases} onChange={(e) => setBasic({ ...basic, aliases: e.target.value })} />
            </Field>
            <Field label="说明">
              <Input value={basic.description} onChange={(e) => setBasic({ ...basic, description: e.target.value })} />
            </Field>
          </div>
          {!readOnly && (
            <div className="mt-2 flex justify-end">
              <Button size="sm" variant="primary" loading={saveBasic.isPending} disabled={!basic.name.trim()} onClick={() => saveBasic.mutate()}>
                保存基本信息
              </Button>
            </div>
          )}
        </section>

        {/* 左=导航树(字段+派生+层级),右=编辑器;两栏各自滚动,弹窗高度固定。 */}
        <div className="flex min-h-0 flex-1 flex-col gap-4 md:flex-row">
          <section className="min-h-0 min-w-0 flex-1 overflow-y-auto pr-1 md:flex-[3_1_460px]">
            <div className="mb-2 flex items-baseline justify-between">
              <span className="font-semibold text-slate-700">字段与派生</span>
              {!readOnly && <span className="text-[11px] text-slate-400">点一行编辑</span>}
            </div>
            <table className="w-full text-left">
              <thead className="sticky top-0 z-10 bg-white text-[11px] uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="whitespace-nowrap py-1 pr-3">名称</th>
                  <th className="whitespace-nowrap py-1 pr-3">列</th>
                  <th className="py-1 pr-3">角色</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {fields.map((field) => {
                  const role = fieldRoleVisual(field);
                  const selected = isSelected('field', field.id);
                  const derived = derivedFromField(field.id, { dimensions, metrics });
                  return (
                    <Fragment key={field.id}>
                    <tr
                      onClick={readOnly || deletionBusy ? undefined : () => setSelection({ kind: 'field', id: field.id })}
                      className={cx(
                        'transition-colors',
                        !readOnly && !deletionBusy && 'cursor-pointer hover:bg-slate-50',
                        selected && 'bg-sky-50',
                      )}
                    >
                      <td className="whitespace-nowrap py-1.5 pr-3 font-medium text-slate-800">{field.name}</td>
                      <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-slate-500">
                        {field.column} <span className="text-slate-300">{field.data_type}</span>
                      </td>
                      <td className="whitespace-nowrap py-1.5 pr-3">
                        <Badge tone={role.tone} variant={role.variant}>
                          {role.icon === 'clock' && <Clock className="h-3 w-3" />}
                          {role.label}
                          {field.kind === 'measure' && field.default_aggregation ? ` · ${field.default_aggregation.toUpperCase()}` : ''}
                        </Badge>
                        {blockersByField.has(field.id) && (
                          <span
                            className="ml-1 inline-flex align-middle"
                            title={`指标口径引用中:${blockersByField
                              .get(field.id)!
                              .map((m) => m.name)
                              .join('、')};改角色前先处理这些指标`}
                          >
                            <Lock className="h-3 w-3 text-slate-400" />
                          </span>
                        )}
                      </td>
                    </tr>
                    {derived.dimensions.map((d) => derivedRow('dimension', d, null, true))}
                    {derived.metrics.map((m) =>
                      derivedRow('metric', m, m.aggregation?.toUpperCase() ?? m.kind, true),
                    )}
                    </Fragment>
                  );
                })}
                {(detachedDimensions.length > 0 || detachedMetrics.length > 0) && (
                  <>
                    <tr>
                      <td colSpan={3} className="pb-1 pt-3 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                        组合指标 / 表达式维度
                      </td>
                    </tr>
                    {detachedDimensions.map((d) => derivedRow('dimension', d, null, false))}
                    {detachedMetrics.map((m) =>
                      derivedRow('metric', m, m.aggregation?.toUpperCase() ?? m.kind, false),
                    )}
                  </>
                )}
              </tbody>
            </table>

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <span className="font-semibold text-slate-700">
            维度层级 {modelHierarchies.length > 0 ? modelHierarchies.length : ''}
          </span>
          {!readOnly && dimensions.length >= 2 && (
            <button
              type="button"
              disabled={deletionBusy}
              className="text-[11px] text-blue-600 hover:underline"
              onClick={() => setSelection({ kind: 'new-hierarchy' })}
            >
              + 新建层级
            </button>
          )}
        </div>
        {modelHierarchies.length === 0 ? (
          <div className="rounded-md border border-dashed border-slate-200 px-3 py-2 text-[11px] text-slate-400">
            把同一把尺子的维度按由粗到细排成层级（如 国家 → 省 → 市），「按地区看」「再细一层」才有落点。
          </div>
        ) : (
          <ul className="divide-y divide-slate-100 overflow-hidden rounded-md border border-slate-200 bg-white/60">
            {modelHierarchies.map((h) =>
              listRow(
                h.id,
                isSelected('hierarchy', h.id),
                () => setSelection({ kind: 'hierarchy', id: h.id }),
                <div className="min-w-0">
                  <div className="truncate text-slate-800">{h.name}</div>
                  <div className="truncate text-[11px] text-slate-400">
                    {h.levels.map((id) => dimensionNameById.get(id) ?? id).join(' → ')}
                  </div>
                </div>,
              ),
            )}
          </ul>
        )}
      </div>
          </section>

          <aside className="min-h-0 min-w-0 flex-1 overflow-y-auto md:flex-[2_1_380px] rounded-lg border border-slate-200 bg-slate-50/70 p-3">
            {detail.kind === 'overview' ? (
              <div className="text-slate-400">
                {readOnly
                  ? '只读模式。左侧是该实体的字段、派生维度指标与层级一览。'
                  : '该实体还没有字段。'}
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                <div className="flex items-center justify-between gap-2 border-b border-slate-200 pb-2">
                  <span className="truncate text-[13px] font-semibold text-slate-700">{detailTitle}</span>
                  {detail.kind === 'metric' && (
                    <ContextualTermButton
                      context={{ projectId, revision, acceptRevision, readOnly }}
                      preset={{ metricId: detail.metric.id }}
                    />
                  )}
                  {detail.kind === 'dimension' && (
                    <ContextualTermButton
                      context={{ projectId, revision, acceptRevision, readOnly }}
                      preset={{ dimensionId: detail.dimension.id }}
                    />
                  )}
                </div>

                {detail.kind === 'missing' && <div className="text-red-600">{detail.message}</div>}

                {detail.kind === 'field' && (
                  <FieldEditor
                      key={`${detail.field.id}:${editEpoch}`}
                      field={detail.field}
                      blockers={blockersByField.get(detail.field.id) ?? []}
                      relations={relationReferences(detail.field, spec.relations).map((r) => {
                        const left = modelNameById.get(r.left_model_id) ?? r.left_model_id;
                        const right = modelNameById.get(r.right_model_id) ?? r.right_model_id;
                        return `${left} — ${right}`;
                      })}
                      onJumpMetric={(id) => setSelection({ kind: 'metric', id })}
                      saving={saveField.isPending}
                      onClose={cancelEdit}
                    onSave={(input) => saveField.mutate({ field: detail.field, input })}
                  />
                )}

                {detail.kind === 'hierarchy' && (
                  <HierarchyEditor
                    key={`${detail.hierarchy?.id ?? 'new'}:${editEpoch}`}
                    initial={hierarchyInitial(detail.hierarchy)}
                    dimensions={dimensions}
                    saving={saveHier.isPending || deletionBusy}
                    onClose={cancelEdit}
                    onDelete={
                      detail.hierarchy && !deletionBusy
                        ? () => void requestDeletion({
                            kind: 'hierarchies',
                            id: detail.hierarchy!.id,
                            label: '层级',
                            name: detail.hierarchy!.name,
                          })
                        : undefined
                    }
                    onSave={(values: HierarchyEditorValues) => {
                      const base: AnalyticsCatalogHierarchy = detail.hierarchy ?? {
                        id: newResourceId('hierarchy'),
                        modelId,
                        name: '',
                        alias: null,
                        levels: [],
                      };
                      saveHier.mutate(applyHierarchyValues(base, values));
                    }}
                  />
                )}

                {detail.kind === 'dimension' && (
                  <DimensionEditor
                    key={`${detail.dimension.id}:${editEpoch}`}
                    dimension={detail.dimension}
                    columns={metricSources.fieldColumns}
                    saving={saveDim.isPending || deletionBusy}
                    aliasSuggest={(current, apply) => (
                      <AliasSuggestButton
                        projectId={projectId}
                        revisionId={revision.id}
                        revisionEtag={revision.etag}
                        resourceType="dimension"
                        modelId={detail.dimension.modelId}
                        name={detail.dimension.name}
                        bizName={detail.dimension.bizName || detail.dimension.expr}
                        description={detail.dimension.description ?? ''}
                        currentAliases={current}
                        onSuggest={apply}
                      />
                    )}
                    dictionary={
                      <DimensionDictionarySection
                        projectId={projectId}
                        revision={revision}
                        dimensionId={detail.dimension.id}
                        acceptRevision={acceptRevision}
                        readOnly={readOnly}
                      />
                    }
                    onClose={cancelEdit}
                    onDelete={
                      deletionBusy
                        ? undefined
                        : () => void requestDeletion({
                            kind: 'dimensions',
                            id: detail.dimension.id,
                            label: '维度',
                            name: detail.dimension.name,
                          })
                    }
                    onSave={(values) =>
                      saveDim.mutate(
                        applyDimensionEditorValues(detail.dimension, values, metricSources.fieldColumns),
                      )
                    }
                  />
                )}

                {detail.kind === 'metric' && (
                  <MetricEditor
                    key={`${detail.metric.id}:${editEpoch}`}
                    metric={detail.metric}
                    spec={spec}
                    aliasSuggest={(current, apply) => (
                      <AliasSuggestButton
                        projectId={projectId}
                        revisionId={revision.id}
                        revisionEtag={revision.etag}
                        resourceType="metric"
                        modelId={detail.metric.modelId}
                        name={detail.metric.name}
                        bizName={detail.metric.bizName}
                        description={detail.metric.description ?? ''}
                        currentAliases={current}
                        onSuggest={apply}
                      />
                    )}
                    sources={{
                      ...metricSources,
                      metrics: metricSources.metrics.filter((m) => m.id !== detail.metric.id),
                    }}
                    saving={saveMet.isPending || deletionBusy}
                    onClose={cancelEdit}
                    onDelete={
                      deletionBusy
                        ? undefined
                        : () => void requestDeletion({
                            kind: 'metrics',
                            id: detail.metric.id,
                            label: '指标',
                            name: detail.metric.name,
                          })
                    }
                    onSave={(values) =>
                      saveMet.mutate(applyMetricEditorValues(detail.metric, values, metricSources))
                    }
                  />
                )}
              </div>
            )}
          </aside>
        </div>
      </div>
      </Dialog>
      <ConfirmationDialog
        open={pendingDeletion !== null}
        title={pendingDeletion ? `确认删除“${pendingDeletion.name}”` : '确认删除'}
        description={pendingDeletion ? `将删除${pendingDeletion.label}“${pendingDeletion.name}”。` : undefined}
        confirmText="确认删除"
        danger
        loading={remove.isPending}
        onClose={cancelDeletionReview}
        onConfirm={() => {
          if (pendingDeletion) remove.mutate(pendingDeletion);
        }}
      >
        {pendingDeletion && (
          <>
            {pendingDeletion.lines.length > 0 ? (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900">
                <div className="font-medium">删除后还将同步处理：</div>
                <ul className="mt-1 list-disc space-y-0.5 pl-4">
                  {pendingDeletion.lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-slate-500">
                未发现其他关联资源。
              </div>
            )}
            <div className="mt-2 text-[11px] text-slate-400">
              影响清单已绑定当前草稿；目录发生变化后，本次确认会自动失效。
            </div>
          </>
        )}
      </ConfirmationDialog>
    </>
  );
}

export function FieldEditor({
  field,
  blockers,
  relations,
  onJumpMetric,
  saving,
  onClose,
  onSave,
}: {
  field: AnalyticsField;
  /** 改角色会被服务端编译拒绝的原因:MEASURE 型指标口径还引用着该 measure。 */
  blockers: AnalyticsCatalogMetric[];
  /** 引用该字段的关系(可改,仅提示)。 */
  relations: string[];
  onJumpMetric: (id: string) => void;
  saving: boolean;
  onClose: () => void;
  onSave: (input: CatalogFieldRoleInput) => void;
}) {
  const locked = blockers.length > 0;
  const [form, setForm] = useState<CatalogFieldRoleInput>({
    name: field.name,
    kind: field.kind,
    identifierType: field.identifier_type ?? 'primary',
    dimensionType: field.dimension_type === 'partition_time' || field.dimension_type === 'time' ? field.dimension_type : 'categorical',
    aggregation: field.default_aggregation ?? 'sum',
    unit: field.unit ?? '',
    createDimension: field.create_dimension,
    createMetric: field.create_metric,
  });
  return (
    <div className="flex flex-col gap-3">
        <Field label="业务名称">
          <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="角色">
          <Select disabled={locked} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Kind })}>
            {KIND_OPTIONS.map((k) => (
              <option key={k.value} value={k.value}>{k.label}</option>
            ))}
          </Select>
        </Field>
        {locked && (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
            <div className="mb-1">改角色会使下列指标的口径失去来源，保存会被拒绝。先调整或删除它们：</div>
            <div className="flex flex-wrap gap-1.5">
              {blockers.map((metric) => (
                <button key={metric.id} type="button" onClick={() => onJumpMetric(metric.id)}>
                  <Badge tone="green" variant="outline">{metric.name}</Badge>
                </button>
              ))}
            </div>
          </div>
        )}
        {relations.length > 0 && (
          <div className="text-[11px] text-slate-500">
            该字段是 {relations.length} 条关系的 join 条件（{relations.join('、')}）。改角色不影响已有关系，但新角色若不是标识，画布上将无法再用它建关系。
          </div>
        )}
        {form.kind === 'identifier' && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
            <Field label="标识类型">
              <Select disabled={locked} value={form.identifierType} onChange={(e) => setForm({ ...form, identifierType: e.target.value as 'primary' | 'foreign' })}>
                <option value="primary">主标识</option>
                <option value="foreign">外部标识</option>
              </Select>
            </Field>
            <label className="flex items-end gap-2 pb-2 text-xs text-slate-600">
              <input type="checkbox" checked={Boolean(form.createDimension)} onChange={(e) => setForm({ ...form, createDimension: e.target.checked })} />
              生成维度
            </label>
          </div>
        )}
        {(form.kind === 'dimension' || form.kind === 'time') && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3">
            <Field label="维度类型">
              <Select
                disabled={locked}
                value={form.dimensionType}
                onChange={(e) => setForm({ ...form, dimensionType: e.target.value as CatalogFieldRoleInput['dimensionType'] })}
              >
                {form.kind === 'time' ? (
                  <>
                    <option value="time">时间</option>
                    <option value="partition_time">分区时间</option>
                  </>
                ) : (
                  <option value="categorical">分类</option>
                )}
              </Select>
            </Field>
            <label className="flex items-end gap-2 pb-2 text-xs text-slate-600">
              <input type="checkbox" checked={Boolean(form.createDimension)} onChange={(e) => setForm({ ...form, createDimension: e.target.checked })} />
              生成维度
            </label>
          </div>
        )}
        {form.kind === 'measure' && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-3">
            <Field label="默认聚合">
              <Select disabled={locked} value={form.aggregation} onChange={(e) => setForm({ ...form, aggregation: e.target.value as CatalogFieldRoleInput['aggregation'] })}>
                {AGG_OPTIONS.map((a) => (
                  <option key={a} value={a}>{a.toUpperCase()}</option>
                ))}
              </Select>
            </Field>
            <Field label="单位">
              <Input value={form.unit ?? ''} onChange={(e) => setForm({ ...form, unit: e.target.value })} placeholder="元 / 件" />
            </Field>
            <label className="flex items-end gap-2 pb-2 text-xs text-slate-600">
              <input type="checkbox" checked={Boolean(form.createMetric)} onChange={(e) => setForm({ ...form, createMetric: e.target.checked })} />
              生成指标
            </label>
          </div>
      )}
      <div className="mt-1 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          取消
        </Button>
        <Button
          variant="primary"
          loading={saving}
          disabled={!form.name.trim()}
          onClick={() => onSave({ ...form, unit: form.unit || undefined })}
        >
          保存
        </Button>
      </div>
    </div>
  );
}
