import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '@analytics/api/client';
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  deleteRelation,
  getGraphLayout,
  newResourceId,
  saveGraphLayout,
  saveRelation,
  versionOf,
} from '@analytics/api/analytics';
import type {
  AnalyticsCatalogRelation,
  AnalyticsModelGraphLayout,
  AnalyticsRelation,
} from '@analytics/api/types';
import { Button, Dialog, Field, Select, Spinner, useToast } from '@analytics/components/ui';
import {
  CARDINALITY_OPTIONS,
  describeError,
  fieldRoleLabel,
  inferCardinality,
} from '@analytics/lib/labels';
import type { WorkbenchContext } from './index';
import { EntityEditor } from './entity-editor';
import { ModelGraph } from './model-graph';
import { availableCanvasHeight } from '@analytics/lib/layout';

type Cardinality = AnalyticsRelation['cardinality'];

interface RelationDraft {
  id?: string;
  left_model_id: string;
  right_model_id: string;
  join_type: 'inner' | 'left' | 'right' | 'full';
  cardinality: Cardinality;
  conditions: Array<{ left_field_id: string; right_field_id: string }>;
}

const JOIN_TYPES: Array<{ value: RelationDraft['join_type']; label: string }> = [
  { value: 'inner', label: 'INNER JOIN' },
  { value: 'left', label: 'LEFT JOIN' },
  { value: 'right', label: 'RIGHT JOIN' },
  { value: 'full', label: 'FULL JOIN' },
];

function useAvailableCanvasHeight(enabled: boolean, measurementKey: string) {
  const ref = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number>();

  useLayoutEffect(() => {
    if (!enabled) return;
    let frame = 0;
    const measure = () => {
      const node = ref.current;
      if (!node) return;
      const visualViewport = window.visualViewport;
      const next = availableCanvasHeight(
        visualViewport?.height ?? window.innerHeight,
        node.getBoundingClientRect().top - (visualViewport?.offsetTop ?? 0),
      );
      setHeight((current) => (current === next ? current : next));
    };
    const scheduleMeasure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(measure);
    };

    measure();
    window.addEventListener('resize', scheduleMeasure);
    window.visualViewport?.addEventListener('resize', scheduleMeasure);
    const observer =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(scheduleMeasure);
    const parent = ref.current?.parentElement;
    let ancestor = parent;
    for (let depth = 0; ancestor && depth < 3; depth += 1) {
      observer?.observe(ancestor);
      ancestor = ancestor.parentElement;
    }
    const mutationObserver =
      typeof MutationObserver === 'undefined'
        ? null
        : new MutationObserver(scheduleMeasure);
    if (parent) mutationObserver?.observe(parent, { childList: true });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('resize', scheduleMeasure);
      window.visualViewport?.removeEventListener('resize', scheduleMeasure);
      observer?.disconnect();
      mutationObserver?.disconnect();
    };
  }, [enabled, measurementKey]);

  return { ref, height };
}

export function CanvasPanel({ projectId, revision, acceptRevision, readOnly }: WorkbenchContext) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const spec = revision.semantic_spec;
  const catalogRelations = revision.semantic_catalog.modelRelations;
  const modelById = useMemo(() => new Map(spec.models.map((m) => [m.id, m])), [spec.models]);
  const fieldById = useMemo(() => new Map(spec.fields.map((f) => [f.id, f])), [spec.fields]);
  const catalogRelationById = useMemo(
    () => new Map(catalogRelations.map((r) => [r.id, r])),
    [catalogRelations],
  );
  const unconfirmed = useMemo(
    () =>
      new Set(
        catalogRelations
          .filter((r) => r.knowflowCardinality === null || r.knowflowCardinality === undefined)
          .map((r) => r.id),
      ),
    [catalogRelations],
  );

  // --- layout -------------------------------------------------------------
  const layoutQuery = useQuery({
    queryKey: ['layout', projectId, revision.id],
    queryFn: () => getGraphLayout(projectId, revision.id),
  });
  const canvasViewport = useAvailableCanvasHeight(
    !layoutQuery.isPending,
    `${revision.id}:${revision.etag}:${readOnly}`,
  );
  const layoutEtag = useRef<number | null>(null);
  const persistLayout = useCallback(
    async (
      positions: AnalyticsModelGraphLayout['positions'],
      viewport: AnalyticsModelGraphLayout['viewport'],
    ) => {
      const etag = layoutEtag.current ?? layoutQuery.data?.etag ?? 0;
      try {
        const saved = await saveGraphLayout(projectId, revision.id, { expected_etag: etag, positions, viewport });
        layoutEtag.current = saved.etag;
        // 缓存要说真话:否则实体编辑后重新派生节点时,画布会跳回还没排过的位置,
        // 而"有模型没坐标"的判断也会继续为真。
        queryClient.setQueryData(['layout', projectId, revision.id], saved);
      } catch (error) {
        // Another tab saved first: pick up its etag so the next drag succeeds.
        if (error instanceof ApiError && error.isConflict) {
          try {
            layoutEtag.current = (await getGraphLayout(projectId, revision.id)).etag;
          } catch {
            // Still a convenience; the next open re-tidies.
          }
        }
      }
    },
    [layoutQuery.data?.etag, projectId, queryClient, revision.id],
  );

  // --- relation editing ---------------------------------------------------
  const [draft, setDraft] = useState<RelationDraft | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);

  const openRelation = useCallback(
    (relationId: string) => {
      const relation = spec.relations.find((r) => r.id === relationId);
      if (!relation) return;
      setDraft({
        id: relation.id,
        left_model_id: relation.left_model_id,
        right_model_id: relation.right_model_id,
        join_type: relation.join_type,
        cardinality: unconfirmed.has(relation.id)
          ? inferCardinality(relation, spec.fields)
          : relation.cardinality,
        conditions: relation.conditions.map((c) => ({ ...c })),
      });
    },
    [spec.fields, spec.relations, unconfirmed],
  );

  const createRelation = useCallback(
    (from: string, to: string, fromField?: string, toField?: string) => {
      if (readOnly) return;
      const conditions = fromField && toField ? [{ left_field_id: fromField, right_field_id: toField }] : [];
      setDraft({
        left_model_id: from,
        right_model_id: to,
        join_type: 'inner',
        cardinality: inferCardinality({ conditions }, spec.fields),
        conditions,
      });
    },
    [readOnly, spec.fields],
  );

  const toCatalog = (input: RelationDraft): AnalyticsCatalogRelation => {
    const previous = input.id ? catalogRelationById.get(input.id) : undefined;
    return {
      ...previous,
      id: input.id ?? newResourceId('relation'),
      fromModelId: input.left_model_id,
      toModelId: input.right_model_id,
      joinType: `${input.join_type} join`,
      joinConditions: input.conditions.map((condition) => ({
        leftField: fieldById.get(condition.left_field_id)?.column ?? '',
        rightField: fieldById.get(condition.right_field_id)?.column ?? '',
        operator: '=',
      })),
      knowflowCardinality: input.cardinality,
      knowflowEvidence: previous?.knowflowEvidence ?? 'name_convention',
      knowflowRationale: previous?.knowflowRationale ?? '',
    };
  };

  const save = useMutation({
    mutationFn: (input: RelationDraft) =>
      saveRelation(projectId, revision.id, versionOf(revision), toCatalog(input)),
    onSuccess: (next) => {
      acceptRevision(next);
      setDraft(null);
      toast.success('关系已保存。');
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const remove = useMutation({
    mutationFn: (relationId: string) => deleteRelation(projectId, revision.id, versionOf(revision), relationId),
    onSuccess: (next) => {
      acceptRevision(next);
      setDraft(null);
      toast.success('关系已删除。');
    },
    onError: (error) => toast.error(describeError(error)),
  });

  // Confirm every pending cardinality in one go, serially because each save
  // advances the etag. Inferred from identifier roles, same as the dialog.
  const confirmAll = useMutation({
    mutationFn: async () => {
      let current = revision;
      let count = 0;
      for (const relation of spec.relations) {
        if (!unconfirmed.has(relation.id)) continue;
        const previous = catalogRelationById.get(relation.id);
        if (!previous) continue;
        current = await saveRelation(projectId, current.id, versionOf(current), {
          ...previous,
          knowflowCardinality: inferCardinality(relation, spec.fields),
        });
        acceptRevision(current);
        count += 1;
      }
      return count;
    },
    onSuccess: (count) => toast.success(`已确认 ${count} 条关系的基数。`),
    onError: (error) => toast.error(describeError(error)),
  });

  if (layoutQuery.isPending) return <Spinner />;
  const layout: AnalyticsModelGraphLayout = layoutQuery.data ?? {
    project_id: projectId,
    revision_id: revision.id,
    etag: 0,
    positions: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    updated_by: null,
    updated_at: '',
  };

  const identifiersOf = (modelId: string) =>
    spec.fields.filter((f) => f.model_id === modelId && f.kind === 'identifier');
  const modelDetail = selectedModel ? modelById.get(selectedModel) : undefined;

  return (
    <div
      ref={canvasViewport.ref}
      className="grid grid-cols-[240px_1fr]"
      style={{ height: canvasViewport.height ?? 640 }}
    >
      <aside className="overflow-auto border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-3 py-2">
          <div className="text-[11px] font-semibold tracking-wide text-slate-400">
            业务实体 {spec.models.length}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-1.5 text-[11px] text-slate-400">
            <span>{spec.relations.length} 条关系</span>
            <span className="text-slate-300">·</span>
            <span>{spec.dimensions.length} 维度 · {spec.metrics.length} 指标</span>
          </div>
          {unconfirmed.size > 0 && !readOnly && (
            <Button
              size="sm"
              variant="primary"
              className="mt-2 w-full"
              loading={confirmAll.isPending}
              onClick={() => confirmAll.mutate()}
            >
              一键确认 {unconfirmed.size} 条关系基数
            </Button>
          )}
        </div>
        {spec.models.map((model) => (
          <button
            key={model.id}
            type="button"
            onClick={() => setSelectedModel(model.id)}
            className={`block w-full border-l-2 px-3 py-1.5 text-left ${
              model.id === selectedModel ? 'border-l-blue-500 bg-blue-50' : 'border-l-transparent hover:bg-slate-50'
            }`}
          >
            <div className="truncate text-[13px] font-medium text-slate-800">{model.name}</div>
            <div className="mt-px flex gap-1.5 text-[11px] text-slate-400">
              <span>{spec.dimensions.filter((d) => d.model_id === model.id).length} 维度</span>
              <span>{spec.metrics.filter((m) => m.model_id === model.id).length} 指标</span>
            </div>
          </button>
        ))}
      </aside>
      <ModelGraph
        revision={revision}
        unconfirmedRelationIds={unconfirmed}
        layout={layout}
        onOpenModel={setSelectedModel}
        onOpenRelation={openRelation}
        onCreateRelation={createRelation}
        onSaveLayout={persistLayout}
      />

      <Dialog
        open={Boolean(draft)}
        title={draft?.id ? '编辑关系' : '新建关系'}
        onClose={() => setDraft(null)}
        width="max-w-xl"
        footer={
          draft && (
            <>
              {draft.id && !readOnly && (
                <Button
                  variant="danger"
                  className="mr-auto"
                  loading={remove.isPending}
                  onClick={() => remove.mutate(draft.id!)}
                >
                  删除
                </Button>
              )}
              <Button onClick={() => setDraft(null)}>取消</Button>
              <Button
                variant="primary"
                disabled={readOnly || draft.conditions.length === 0 || draft.conditions.some((c) => !c.left_field_id || !c.right_field_id)}
                loading={save.isPending}
                onClick={() => save.mutate(draft)}
              >
                保存
              </Button>
            </>
          )
        }
      >
        {draft && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
              <Field label="左侧实体">
                <Select
                  value={draft.left_model_id}
                  onChange={(e) => setDraft({ ...draft, left_model_id: e.target.value, conditions: [] })}
                >
                  {spec.models.map((m) => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </Select>
              </Field>
              <Field label="右侧实体">
                <Select
                  value={draft.right_model_id}
                  onChange={(e) => setDraft({ ...draft, right_model_id: e.target.value, conditions: [] })}
                >
                  {spec.models.map((m) => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </Select>
              </Field>
            </div>
            <div>
              <div className="mb-1 flex items-center justify-between text-xs font-medium text-slate-600">
                Join 条件（仅标识字段）
                <button
                  type="button"
                  className="text-blue-600 hover:text-blue-500"
                  onClick={() =>
                    setDraft({ ...draft, conditions: [...draft.conditions, { left_field_id: '', right_field_id: '' }] })
                  }
                >
                  + 添加
                </button>
              </div>
              {draft.conditions.length === 0 && (
                <div className="rounded-md border border-dashed border-slate-200 px-3 py-2 text-xs text-slate-400">
                  至少需要一个等值条件。
                </div>
              )}
              {draft.conditions.map((condition, index) => (
                <div key={index} className="mb-2 grid grid-cols-[1fr_auto_1fr_auto] items-center gap-2">
                  <Select
                    value={condition.left_field_id}
                    onChange={(e) => {
                      const conditions = draft.conditions.map((c, i) => (i === index ? { ...c, left_field_id: e.target.value } : c));
                      setDraft({ ...draft, conditions, cardinality: inferCardinality({ conditions }, spec.fields) });
                    }}
                  >
                    <option value="">选择字段</option>
                    {identifiersOf(draft.left_model_id).map((f) => (
                      <option key={f.id} value={f.id}>{f.name}（{fieldRoleLabel(f)}）</option>
                    ))}
                  </Select>
                  <span className="text-slate-400">=</span>
                  <Select
                    value={condition.right_field_id}
                    onChange={(e) => {
                      const conditions = draft.conditions.map((c, i) => (i === index ? { ...c, right_field_id: e.target.value } : c));
                      setDraft({ ...draft, conditions, cardinality: inferCardinality({ conditions }, spec.fields) });
                    }}
                  >
                    <option value="">选择字段</option>
                    {identifiersOf(draft.right_model_id).map((f) => (
                      <option key={f.id} value={f.id}>{f.name}（{fieldRoleLabel(f)}）</option>
                    ))}
                  </Select>
                  <button
                    type="button"
                    className="text-xs text-slate-400 hover:text-red-600"
                    onClick={() => setDraft({ ...draft, conditions: draft.conditions.filter((_, i) => i !== index) })}
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
              <Field label="Join 类型">
                <Select value={draft.join_type} onChange={(e) => setDraft({ ...draft, join_type: e.target.value as RelationDraft['join_type'] })}>
                  {JOIN_TYPES.map((j) => (
                    <option key={j.value} value={j.value}>{j.label}</option>
                  ))}
                </Select>
              </Field>
              <Field label="基数" hint="多对多会让跨关系求和翻倍，发布前会被阻断。">
                <Select value={draft.cardinality} onChange={(e) => setDraft({ ...draft, cardinality: e.target.value as Cardinality })}>
                  {CARDINALITY_OPTIONS.map((c) => (
                    <option key={c.value} value={c.value}>{c.label}</option>
                  ))}
                </Select>
              </Field>
            </div>
          </div>
        )}
      </Dialog>

      {modelDetail && (
        <EntityEditor
          projectId={projectId}
          revision={revision}
          modelId={modelDetail.id}
          readOnly={readOnly}
          acceptRevision={acceptRevision}
          onClose={() => setSelectedModel(null)}
        />
      )}
    </div>
  );
}
