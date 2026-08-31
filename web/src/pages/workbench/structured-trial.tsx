import { useMutation } from '@tanstack/react-query';
import { Play } from 'lucide-react';
import { useMemo, useState } from 'react';
import { newResourceId, previewStructuredQuery, versionOf } from '@analytics/api/analytics';
import type { AnalyticsQueryFilter, AnalyticsSemanticQuery } from '@analytics/api/types';
import { QueryAnswer, type QueryTurn } from '@analytics/components/query-answer';
import { Button, Field, Input, Select, cx } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import type { WorkbenchContext } from './index';

const OPERATORS: Array<{ value: AnalyticsQueryFilter['operator']; label: string; needsValue: boolean }> = [
  { value: 'eq', label: '等于', needsValue: true },
  { value: 'ne', label: '不等于', needsValue: true },
  { value: 'gt', label: '大于', needsValue: true },
  { value: 'gte', label: '大于等于', needsValue: true },
  { value: 'lt', label: '小于', needsValue: true },
  { value: 'lte', label: '小于等于', needsValue: true },
  { value: 'in', label: '属于（逗号分隔）', needsValue: true },
  { value: 'not_in', label: '不属于（逗号分隔）', needsValue: true },
  { value: 'between', label: '介于（逗号分隔两值）', needsValue: true },
  { value: 'like', label: '包含', needsValue: true },
  { value: 'is_null', label: '为空', needsValue: false },
  { value: 'is_not_null', label: '不为空', needsValue: false },
];

interface FilterDraft {
  dimension_id: string;
  operator: AnalyticsQueryFilter['operator'];
  value: string;
}

function parseValue(operator: AnalyticsQueryFilter['operator'], raw: string): unknown {
  const coerce = (text: string) => {
    const trimmed = text.trim();
    return trimmed !== '' && !Number.isNaN(Number(trimmed)) ? Number(trimmed) : trimmed;
  };
  if (operator === 'is_null' || operator === 'is_not_null') return null;
  if (operator === 'in' || operator === 'not_in' || operator === 'between') {
    return raw.split(/[，,]/).map(coerce).filter((v) => v !== '');
  }
  return coerce(raw);
}

/**
 * Structured playground: the modeler picks governed metrics/dimensions and
 * submits a SemanticQuery straight to Corrector → Translator → Executor. A
 * failure here is a modeling or SQL problem; mapping errors cannot occur.
 */
export function StructuredTrial({ projectId, revision }: Pick<WorkbenchContext, 'projectId' | 'revision'>) {
  const spec = revision.semantic_spec;
  const names = useMemo(() => {
    const map = new Map<string, string>();
    spec.dimensions.forEach((d) => map.set(d.id, d.name));
    spec.metrics.forEach((m) => map.set(m.id, m.name));
    spec.datasets.forEach((d) => map.set(d.id, d.name));
    return map;
  }, [spec]);
  const nameOf = (id: string) => names.get(id) ?? id.split(':').pop() ?? id;

  const [datasetId, setDatasetId] = useState(spec.datasets[0]?.id ?? '');
  const dataset = spec.datasets.find((d) => d.id === datasetId);
  const [queryType, setQueryType] = useState<'detail' | 'aggregate'>('aggregate');
  const [metricIds, setMetricIds] = useState<string[]>([]);
  const [dimensionIds, setDimensionIds] = useState<string[]>([]);
  const [filters, setFilters] = useState<FilterDraft[]>([]);
  const [orderBy, setOrderBy] = useState('');
  const [direction, setDirection] = useState<'asc' | 'desc'>('desc');
  const [limit, setLimit] = useState('100');
  const [turns, setTurns] = useState<QueryTurn[]>([]);

  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const switchDataset = (id: string) => {
    setDatasetId(id);
    setMetricIds([]);
    setDimensionIds([]);
    setFilters([]);
    setOrderBy('');
  };

  const run = useMutation({
    mutationFn: ({ turnId, query }: { turnId: string; query: AnalyticsSemanticQuery }) =>
      previewStructuredQuery(projectId, revision.id, versionOf(revision), query).then((response) => ({ turnId, response })),
    onSuccess: ({ turnId, response }) =>
      setTurns((c) => c.map((t) => (t.id === turnId ? { ...t, response, pending: false } : t))),
    onError: (error, { turnId }) =>
      setTurns((c) => c.map((t) => (t.id === turnId ? { ...t, error: describeError(error), pending: false } : t))),
  });

  const submit = () => {
    if (!dataset) return;
    const query: AnalyticsSemanticQuery = {
      dataset_id: dataset.id,
      query_type: queryType,
      metric_ids: metricIds,
      aggregation_overrides: [],
      dimension_ids: dimensionIds,
      filters: filters
        .filter((f) => f.dimension_id)
        .map((f) => ({ dimension_id: f.dimension_id, operator: f.operator, value: parseValue(f.operator, f.value) })),
      measure_filters: [],
      metric_filters: [],
      order_by: orderBy ? [{ element_id: orderBy, direction }] : [],
      limit: limit.trim() ? Number(limit) : null,
    };
    const summary = [
      ...metricIds.map(nameOf),
      ...(dimensionIds.length ? [`按 ${dimensionIds.map(nameOf).join('、')}`] : []),
      ...filters.filter((f) => f.dimension_id).map((f) => `${nameOf(f.dimension_id)} ${OPERATORS.find((o) => o.value === f.operator)?.label} ${f.value}`),
    ].join(' · ');
    const turnId = newResourceId('turn');
    setTurns((c) => [{ id: turnId, question: summary || '（无指标无维度）', pending: true }, ...c]);
    run.mutate({ turnId, query });
  };

  if (!dataset) return <div className="text-xs text-slate-400">当前目录尚无已编译查询作用域。</div>;
  const metrics = spec.metrics.filter((m) => dataset.metric_ids.includes(m.id));
  const dimensions = spec.dimensions.filter((d) => dataset.dimension_ids.includes(d.id));
  const chip = (active: boolean) =>
    cx(
      'rounded-full border px-2.5 py-0.5 text-xs transition-colors',
      active ? 'border-blue-500 bg-blue-50 text-blue-700' : 'border-slate-200 text-slate-600 hover:border-slate-300',
    );
  const canRun = metricIds.length > 0 || dimensionIds.length > 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        <Field label="查询作用域" hint="只读编译产物，仅用于兼容结构化查询接口。">
          <Select value={datasetId} onChange={(e) => switchDataset(e.target.value)}>
            {spec.datasets.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </Select>
        </Field>
        <Field label="查询类型">
          <Select value={queryType} onChange={(e) => setQueryType(e.target.value as 'detail' | 'aggregate')}>
            <option value="aggregate">聚合</option>
            <option value="detail">明细</option>
          </Select>
        </Field>
        <Field label="行数上限">
          <Input value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ''))} />
        </Field>
      </div>
      <div>
        <div className="mb-1.5 text-xs font-medium text-slate-600">指标 {metricIds.length ? `(${metricIds.length})` : ''}</div>
        <div className="flex flex-wrap gap-1.5">
          {metrics.map((m) => (
            <button key={m.id} type="button" className={chip(metricIds.includes(m.id))} onClick={() => setMetricIds(toggle(metricIds, m.id))}>
              {m.name}
            </button>
          ))}
          {metrics.length === 0 && <span className="text-xs text-slate-400">该作用域没有指标</span>}
        </div>
      </div>
      <div>
        <div className="mb-1.5 text-xs font-medium text-slate-600">维度 {dimensionIds.length ? `(${dimensionIds.length})` : ''}</div>
        <div className="flex flex-wrap gap-1.5">
          {dimensions.map((d) => (
            <button key={d.id} type="button" className={chip(dimensionIds.includes(d.id))} onClick={() => setDimensionIds(toggle(dimensionIds, d.id))}>
              {d.name}
              {d.metric_time_axis ? (
                <span
                  className="ml-1 rounded bg-violet-100 px-1 text-[10px] text-violet-700 dark:bg-violet-500/20 dark:text-violet-300"
                  title="系统自动生成:所选指标声明了不同的时间轴,按此维度分组时各指标按各自的轴统计"
                >
                  自动生成
                </span>
              ) : null}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="mb-1.5 flex items-center justify-between text-xs font-medium text-slate-600">
          过滤条件
          <button
            type="button"
            className="text-blue-600 hover:text-blue-500"
            onClick={() => setFilters([...filters, { dimension_id: dimensions[0]?.id ?? '', operator: 'eq', value: '' }])}
          >
            + 添加
          </button>
        </div>
        {filters.map((f, index) => {
          const op = OPERATORS.find((o) => o.value === f.operator);
          const update = (patch: Partial<FilterDraft>) => setFilters(filters.map((x, i) => (i === index ? { ...x, ...patch } : x)));
          return (
            <div key={index} className="mb-2 grid grid-cols-[1fr_1fr_1fr_auto] items-center gap-2">
              <Select value={f.dimension_id} onChange={(e) => update({ dimension_id: e.target.value })}>
                {dimensions.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </Select>
              <Select value={f.operator} onChange={(e) => update({ operator: e.target.value as AnalyticsQueryFilter['operator'] })}>
                {OPERATORS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </Select>
              <Input disabled={!op?.needsValue} value={f.value} onChange={(e) => update({ value: e.target.value })} placeholder="值" />
              <button type="button" className="text-xs text-slate-400 hover:text-red-600" onClick={() => setFilters(filters.filter((_, i) => i !== index))}>
                移除
              </button>
            </div>
          );
        })}
      </div>
      <div className="grid grid-cols-[1fr_140px_auto] items-end gap-2">
        <Field label="排序">
          <Select value={orderBy} onChange={(e) => setOrderBy(e.target.value)}>
            <option value="">不排序</option>
            {[...metricIds, ...dimensionIds].map((id) => (
              <option key={id} value={id}>{nameOf(id)}</option>
            ))}
          </Select>
        </Field>
        <Field label="方向">
          <Select value={direction} onChange={(e) => setDirection(e.target.value as 'asc' | 'desc')}>
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </Select>
        </Field>
        <Button variant="primary" icon={<Play className="h-3.5 w-3.5" />} disabled={!canRun} loading={run.isPending} onClick={submit}>
          执行
        </Button>
      </div>
      <div className="flex flex-col gap-4">
        {turns.map((turn) => (
          <div key={turn.id} className="rounded-lg border border-slate-200 p-3">
            <div className="mb-2 text-xs font-medium text-slate-700">{turn.question}</div>
            <QueryAnswer projectId={projectId} turn={turn} columnName={nameOf} onChoose={() => undefined} />
          </div>
        ))}
      </div>
    </div>
  );
}
