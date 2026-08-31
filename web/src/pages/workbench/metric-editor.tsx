import { useMemo, useState } from 'react';
import type {
  AnalyticsCatalogMetric,
  AnalyticsCatalogMetricDefineType,
  AnalyticsSemanticSpec,
} from '@analytics/api/types';
import { Button, Field, Input, Select, Textarea } from '@analytics/components/ui';
import {
  DEFINE_TYPE_EXAMPLES,
  DEFINE_TYPE_RULE,
  type MetricDefinitionSources,
  activeParams,
  buildDefineParams,
  checkDefinition,
} from './metric-definition';

/**
 * 指标编辑:治理属性与口径定义都可改。
 *
 * 口径(定义方式、表达式、固定过滤)由 AI 建模生成,但 AI 会认错列、会漏掉
 * 「已扣退款」这类业务约定,所以必须留人工修正的入口。来源度量/字段不单独做
 * 控件——服务端要求来源与表达式引用完全一致,让用户维护两处必然漂移,这里统一
 * 从表达式反推。
 */

const SENSITIVITY = [
  { value: 0, label: '0 · 普通' },
  { value: 1, label: '1 · 内部' },
  { value: 2, label: '2 · 敏感' },
  { value: 3, label: '3 · 高敏感' },
];

const DEFINE_TYPE_LABEL: Record<AnalyticsCatalogMetricDefineType, string> = {
  FIELD: '字段表达式',
  MEASURE: '度量聚合',
  METRIC: '指标组合',
};

const splitList = (text: string) =>
  text.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);

export interface MetricEditorValues {
  metricDefineType: AnalyticsCatalogMetricDefineType;
  aggTimeDimensionId: string;
  expr: string;
  filterSql: string;
  name: string;
  aliases: string;
  description: string;
  sensitiveLevel: number;
  dataFormatType: '' | 'decimal' | 'percent';
  decimalPlaces: number;
  needMultiply100: boolean;
  classifications: string;
}

export function metricEditorInitial(metric: AnalyticsCatalogMetric): MetricEditorValues {
  const params = activeParams(metric);
  return {
    metricDefineType: metric.metricDefineType,
    aggTimeDimensionId: metric.aggTimeDimensionId ?? '',
    expr: params.expr,
    filterSql: params.filterSql,
    name: metric.name,
    aliases: (metric.alias ?? '').split(/[，,]/).filter(Boolean).join('，'),
    description: metric.description ?? '',
    sensitiveLevel: metric.sensitiveLevel ?? 0,
    dataFormatType:
      metric.dataFormatType === 'decimal' || metric.dataFormatType === 'percent'
        ? metric.dataFormatType
        : '',
    decimalPlaces: metric.dataFormat?.decimalPlaces ?? 2,
    needMultiply100: metric.dataFormat?.needMultiply100 ?? false,
    classifications: (metric.classifications ?? []).join('，'),
  };
}

/** 把表单值合回完整 DTO:未编辑的字段一律保留原值,避免静默丢字段。 */
export function applyMetricEditorValues(
  existing: AnalyticsCatalogMetric,
  values: MetricEditorValues,
  sources: MetricDefinitionSources,
): AnalyticsCatalogMetric {
  // 口径非法时原样保留旧定义:反推不出来源就写入,会得到一个空 measures 的
  // params,服务端只会报「至少引用一个来源」,用户却看不出是被这里改坏的。
  const check = checkDefinition(values.metricDefineType, values.expr, sources);
  return {
    ...existing,
    ...(check.error
      ? {}
      : buildDefineParams(
          values.metricDefineType,
          values.expr,
          values.filterSql,
          check.resolved,
          sources,
        )),
    name: values.name.trim(),
    aggTimeDimensionId: values.aggTimeDimensionId || null,
    description: values.description.trim(),
    alias: splitList(values.aliases).join(',') || null,
    sensitiveLevel: values.sensitiveLevel,
    classifications: splitList(values.classifications),
    dataFormatType: values.dataFormatType || null,
    dataFormat: values.dataFormatType
      ? {
          needMultiply100: values.dataFormatType === 'percent' ? values.needMultiply100 : false,
          decimalPlaces: values.decimalPlaces,
        }
      : null,
  };
}

function DefinitionEditor({
  type,
  expr,
  filterSql,
  sources,
  error,
  onChange,
}: {
  type: AnalyticsCatalogMetricDefineType;
  expr: string;
  filterSql: string;
  sources: MetricDefinitionSources;
  error: string | null;
  onChange: (patch: {
    metricDefineType?: AnalyticsCatalogMetricDefineType;
    expr?: string;
    filterSql?: string;
  }) => void;
}) {
  const available =
    type === 'MEASURE'
      ? sources.measures.map((m) => ({ key: m.bizName, label: `${m.bizName}（${m.name}·${m.agg}）` }))
      : type === 'FIELD'
        ? sources.fieldColumns.map((c) => ({ key: c, label: c }))
        : sources.metrics.map((m) => ({ key: m.bizName, label: `${m.bizName}（${m.name}）` }));

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[13px] font-medium text-slate-700">口径定义</span>
        <span className="text-[11px] text-amber-700">改动会直接改变问数结果</span>
      </div>
      <div className="grid grid-cols-[140px_1fr] gap-3">
        <Field label="定义方式">
          <Select
            value={type}
            onChange={(e) =>
              onChange({ metricDefineType: e.target.value as AnalyticsCatalogMetricDefineType })
            }
          >
            {(Object.keys(DEFINE_TYPE_LABEL) as AnalyticsCatalogMetricDefineType[]).map((key) => (
              <option key={key} value={key}>
                {DEFINE_TYPE_LABEL[key]}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="表达式"
          tip={
            <span className="block text-[11px] text-slate-500">
              <span className="block">{DEFINE_TYPE_RULE[type]}</span>
              <span className="mt-1.5 block">
                示例(点击填入):
                {DEFINE_TYPE_EXAMPLES[type].map((example) => (
                  <button
                    key={example}
                    type="button"
                    onClick={() => onChange({ expr: example })}
                    className="mt-1 block rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-left font-mono text-[11px] text-slate-600 hover:border-slate-300 hover:text-slate-800"
                  >
                    {example}
                  </button>
                ))}
              </span>
            </span>
          }
        >
          <Textarea
            rows={2}
            className="font-mono text-[12px]"
            value={expr}
            onChange={(e) => onChange({ expr: e.target.value })}
          />
        </Field>
      </div>
      <Field label="固定过滤" hint="始终附加在该指标上的条件,例如 status = 'paid';留空表示不过滤">
        <Input
          className="font-mono text-[12px]"
          value={filterSql}
          onChange={(e) => onChange({ filterSql: e.target.value })}
        />
      </Field>
      {error && <div className="mt-2 text-[11px] text-red-600">{error}</div>}
      <div className="mt-2 text-[11px] text-slate-500">
        可用来源：
        {available.length === 0 ? (
          <span className="text-slate-400">该模型还没有可用来源</span>
        ) : (
          available.map((item) => (
            <button
              key={item.key}
              type="button"
              title="点击插入到表达式"
              onClick={() => onChange({ expr: `${expr}${expr.trim() ? ' ' : ''}${item.key}` })}
              className="ml-1 rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 hover:border-slate-300"
            >
              {item.label}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

export function MetricEditor({
  metric,
  spec,
  sources,
  saving,
  aliasSuggest,
  onSave,
  onDelete,
  onClose,
}: {
  metric: AnalyticsCatalogMetric;
  spec: AnalyticsSemanticSpec;
  sources: MetricDefinitionSources;
  saving: boolean;
  /** 别名「建议」按钮(需要 revision 上下文,由外层注入)。 */
  aliasSuggest?: (current: string, apply: (merged: string) => void) => React.ReactNode;
  onSave: (values: MetricEditorValues) => void;
  onDelete?: () => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState<MetricEditorValues>(() => metricEditorInitial(metric));
  // 下钻只能选同模型的维度:跨模型下钻会让指标落到无法到达的粒度上。
  const dimensionOptions = useMemo(
    () => spec.dimensions.filter((d) => d.model_id === metric.modelId),
    [spec.dimensions, metric.modelId],
  );
  const set = <K extends keyof MetricEditorValues>(key: K, value: MetricEditorValues[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));
  const definition = useMemo(
    () => checkDefinition(form.metricDefineType, form.expr, sources),
    [form.metricDefineType, form.expr, sources],
  );
  // 时间轴只能选本模型的时间维度:跨模型要先 join,会改变指标的聚合粒度。
  const timeDimensions = useMemo(
    () => dimensionOptions.filter((d) => d.semantic_type === 'time'),
    [dimensionOptions],
  );

  return (
    <div className="flex flex-col gap-3">
      <DefinitionEditor
        type={form.metricDefineType}
        expr={form.expr}
        filterSql={form.filterSql}
        sources={sources}
        error={definition.error}
        onChange={(patch) => setForm((prev) => ({ ...prev, ...patch }))}
      />
      <Field label="业务名称">
        <Input value={form.name} onChange={(e) => set('name', e.target.value)} />
      </Field>
      <Field label="别名" hint="用「，」分隔;问数时用户可能说出的其它叫法">
        <div className="flex gap-2">
          <Input value={form.aliases} onChange={(e) => set('aliases', e.target.value)} />
          {aliasSuggest?.(form.aliases, (merged) => set('aliases', merged))}
        </div>
      </Field>
      <Field label="说明" hint="会进入模型提示,写清口径(如「已扣退款」)">
        <Textarea
          rows={2}
          value={form.description}
          onChange={(e) => set('description', e.target.value)}
        />
      </Field>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
        <Field label="敏感度">
          <Select
            value={String(form.sensitiveLevel)}
            onChange={(e) => set('sensitiveLevel', Number(e.target.value))}
          >
            {SENSITIVITY.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="业务分类" hint="用「，」分隔">
          <Input
            value={form.classifications}
            onChange={(e) => set('classifications', e.target.value)}
          />
        </Field>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        <Field label="展示格式">
          <Select
            value={form.dataFormatType}
            onChange={(e) =>
              set('dataFormatType', e.target.value as MetricEditorValues['dataFormatType'])
            }
          >
            <option value="">原样</option>
            <option value="decimal">小数</option>
            <option value="percent">百分比</option>
          </Select>
        </Field>
        {form.dataFormatType && (
          <Field label="小数位">
            <Input
              type="number"
              min={0}
              max={6}
              value={String(form.decimalPlaces)}
              onChange={(e) => set('decimalPlaces', Number(e.target.value))}
            />
          </Field>
        )}
        {form.dataFormatType === 'percent' && (
          <Field label="数值换算" hint="存的是 0.3 就勾上">
            <label className="flex h-9 items-center gap-2 text-[13px] text-slate-600">
              <input
                type="checkbox"
                checked={form.needMultiply100}
                onChange={(e) => set('needMultiply100', e.target.checked)}
              />
              ×100
            </label>
          </Field>
        )}
      </div>
      {form.metricDefineType !== 'METRIC' && timeDimensions.length > 1 && (
        <Field
          label="聚合时间轴"
          hint="问「本月」时按哪个时间列统计。留空则用数据集的默认时间维度——本模型有多个时间列时，不指定就可能统计错列"
        >
          <Select
            value={form.aggTimeDimensionId}
            onChange={(e) => set('aggTimeDimensionId', e.target.value)}
          >
            <option value="">跟随数据集默认</option>
            {timeDimensions.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <div className="mt-1 flex items-center justify-between">
        {onDelete ? (
          <Button variant="ghost" onClick={onDelete}>
            删除指标
          </Button>
        ) : (
          <span />
        )}
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={saving}
            disabled={!form.name.trim() || definition.error !== null}
            onClick={() => onSave(form)}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  );
}
