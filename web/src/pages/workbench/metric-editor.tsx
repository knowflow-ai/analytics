import { useMemo, useState } from 'react';
import { ChevronRight, Plus, X } from 'lucide-react';
import type {
  AnalyticsCatalogMetric,
  AnalyticsCatalogMetricDefineType,
  AnalyticsSemanticSpec,
} from '@analytics/api/types';
import { Badge, Button, Field, Input, Select, Textarea, cx } from '@analytics/components/ui';
import {
  type MetricDefinitionSources,
  activeParams,
  buildDefineParams,
  checkDefinition,
} from './metric-definition';
import { ExpressionArea } from './expression-field';
import {
  OPERATOR_LABEL,
  type BinaryOperator,
  readBinaryExpr,
  writeBinaryExpr,
} from './expression-builder';
import {
  AGG_LABEL,
  ALL_ROWS,
  FILTER_OP_LABEL,
  type AggName,
  type FilterCondition,
  type FilterOp,
  columnExpr,
  deriveDefineType,
  parseConditions,
  readShape,
  serializeConditions,
} from './metric-authoring';

/**
 * 指标编辑器。
 *
 * 界面上只问一句「这个指标怎么算」——对一列做统计 / 由其它指标计算。目录里的
 * FIELD / MEASURE / METRIC 由系统按这个选择定（``deriveDefineType``），不再让用户
 * 先理解编译器的分类才能建指标。表单值仍是那套规范三元组（定义方式、表达式、
 * 固定过滤），界面只是它的投影：选择器表达不了的写法（跨列表达式、OR 条件）
 * 不悄悄改写，回落成原文编辑，改坏的风险留给写得出那种写法的人。
 *
 * 类型徽章（原子 / 复合）是选择的结果，不是入门的分类题。
 */

const SENSITIVITY = [
  { value: 0, label: '0 · 普通' },
  { value: 1, label: '1 · 内部' },
  { value: 2, label: '2 · 敏感' },
  { value: 3, label: '3 · 高敏感' },
];

const AGGREGATIONS: AggName[] = ['SUM', 'COUNT', 'COUNT_DISTINCT', 'AVG', 'MIN', 'MAX'];
const FILTER_OPS: FilterOp[] = ['=', '!=', '>', '>=', '<', '<=', 'IN'];

const NUMERIC_TYPE = /^(small|big|tiny|medium)?(int|integer|serial|decimal|numeric|real|double|float|money)/i;

/**
 * 新建时预选哪一列。
 *
 * 取第一个字段会得到「账户号 的 求和」这种荒谬默认——它不是错，是在教用户这个表单
 * 不用看。优先选有受治理度量的列（建模已经认定它是可加的数量），其次数值列。
 */
function defaultColumn(
  columns: Array<{ column: string; dataType: string }>,
  sources: MetricDefinitionSources,
): string {
  const governed = new Set(sources.measures.map((m) => m.expr.trim().toLowerCase()));
  return (
    columns.find((item) => governed.has(item.column.toLowerCase()))?.column ??
    columns.find((item) => NUMERIC_TYPE.test(item.dataType))?.column ??
    columns[0]?.column ??
    ''
  );
}

/** 新建指标时的英文标识：其它指标引用它时用这个名字，所以必须是标识符形状。 */
const BIZ_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

const splitList = (text: string) =>
  text.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);

export interface MetricEditorValues {
  metricDefineType: AnalyticsCatalogMetricDefineType;
  aggTimeDimensionId: string;
  expr: string;
  filterSql: string;
  /** 只在新建时可编辑;改已有指标的英文标识会打断引用它的组合指标。 */
  bizName: string;
  name: string;
  aliases: string;
  description: string;
  sensitiveLevel: number;
  dataFormatType: '' | 'decimal' | 'percent';
  decimalPlaces: number;
  needMultiply100: boolean;
  classifications: string;
}

const BLANK: MetricEditorValues = {
  metricDefineType: 'FIELD',
  aggTimeDimensionId: '',
  expr: '',
  filterSql: '',
  bizName: '',
  name: '',
  aliases: '',
  description: '',
  sensitiveLevel: 0,
  dataFormatType: '',
  decimalPlaces: 2,
  needMultiply100: false,
  classifications: '',
};

/** ``null`` = 新建。seed 用来预填从某个字段行进来的那一列。 */
export function metricEditorInitial(
  metric: AnalyticsCatalogMetric | null,
  seed?: { column?: string; shape?: 'column' | 'metric' },
): MetricEditorValues {
  if (metric === null) {
    return {
      ...BLANK,
      metricDefineType: seed?.shape === 'metric' ? 'METRIC' : 'FIELD',
      expr: seed?.column ? `SUM(${seed.column})` : '',
    };
  }
  const params = activeParams(metric);
  return {
    bizName: metric.bizName,
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
    ...(values.bizName.trim() ? { bizName: values.bizName.trim() } : {}),
    // 复合指标的时间轴由它引用的原子指标决定,在这里再声明一次会与依赖冲突
    // (contracts.MetricSpec: only an atomic metric can declare an aggregation
    // time dimension)。切到复合后必须清掉,否则保存必炸。
    aggTimeDimensionId:
      values.metricDefineType === 'METRIC' ? null : values.aggTimeDimensionId || null,
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

function ShapeEditor({
  values,
  columns,
  sources,
  error,
  onChange,
}: {
  values: MetricEditorValues;
  columns: Array<{ column: string; name: string; dataType: string }>;
  sources: MetricDefinitionSources;
  error: string | null;
  onChange: (patch: Partial<MetricEditorValues>) => void;
}) {
  const read = readShape(values.metricDefineType, values.expr, sources);
  const shape = read?.shape ?? (values.metricDefineType === 'METRIC' ? 'metric' : 'column');
  // 选择器表达不了这条口径（跨列表达式这类）：回落成原文编辑，不悄悄改写它。
  const freeform = read === null && values.expr.trim() !== '';

  const pickColumn = (choice: { column: string; aggregation: AggName }) =>
    onChange({
      metricDefineType: deriveDefineType('column', choice, sources),
      expr: columnExpr(choice, sources),
    });

  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-medium text-slate-600">这个指标怎么算？</span>

      <label
        className={cx(
          'flex flex-wrap items-center gap-2.5 rounded-md border bg-white px-2.5 py-2',
          shape === 'column' && !freeform
            ? 'border-blue-300 ring-2 ring-blue-100'
            : 'border-slate-200',
        )}
      >
        <input
          type="radio"
          checked={shape === 'column' && !freeform}
          onChange={() =>
            pickColumn({ column: read?.column ?? columns[0]?.column ?? '', aggregation: 'SUM' })
          }
        />
        <span className="whitespace-nowrap text-[13px] text-slate-800">对一列做统计</span>
        {shape === 'column' && !freeform && (
          <span className="ml-auto flex items-center gap-2">
            <Select
              className="h-8 w-auto"
              value={read?.column ?? ''}
              onChange={(e) =>
                pickColumn({ column: e.target.value, aggregation: read?.aggregation ?? 'SUM' })
              }
            >
              {/* 行数指标在契约里是一等公民（MetricSpec.counts_rows），不是读不懂的表达式。 */}
              <option value={ALL_ROWS}>所有行</option>
              {columns.map((item) => (
                <option key={item.column} value={item.column}>
                  {item.name || item.column}
                </option>
              ))}
            </Select>
            <span className="text-xs text-slate-400">的</span>
            <Select
              className="h-8 w-auto"
              value={read?.aggregation ?? 'SUM'}
              disabled={read?.column === ALL_ROWS}
              onChange={(e) =>
                pickColumn({
                  column: read?.column ?? columns[0]?.column ?? '',
                  aggregation: e.target.value as AggName,
                })
              }
            >
              {(read?.column === ALL_ROWS ? (['COUNT'] as AggName[]) : AGGREGATIONS).map((agg) => (
                <option key={agg} value={agg}>
                  {AGG_LABEL[agg]}
                </option>
              ))}
            </Select>
          </span>
        )}
      </label>

      <label
        className={cx(
          'flex flex-col gap-2 rounded-md border bg-white px-2.5 py-2',
          shape === 'metric' || freeform ? 'border-blue-300 ring-2 ring-blue-100' : 'border-slate-200',
        )}
      >
        <span className="flex items-center gap-2.5">
          <input
            type="radio"
            checked={shape === 'metric' || freeform}
            onChange={() => onChange({ metricDefineType: 'METRIC', expr: '' })}
          />
          <span className="text-[13px] text-slate-800">由其它指标计算</span>
          {freeform && (
            <span className="ml-auto text-[11px] text-amber-700">
              这条口径选择器表达不了，按原文编辑
            </span>
          )}
        </span>
        {freeform ? (
          // 读不回来的口径：直接给原文。绝不能被「该模型还没有其它指标」那句提示吞掉——
          // 实机 COUNT(*) 就是这么整段消失的。
          <ExpressionArea
            value={values.expr}
            tokens={sources.metrics.map((item) => ({
              key: item.id,
              label: item.name,
              token: item.bizName,
            }))}
            emptyHint="该模型还没有其它指标"
            error={error}
            onChange={(expr) => onChange({ expr })}
          />
        ) : (
          shape === 'metric' && (
            <MetricExpression
              values={values}
              sources={sources}
              error={error}
              onChange={onChange}
            />
          )
        )}
      </label>
      {error && <div className="text-[11px] text-red-600">{error}</div>}
    </div>
  );
}

function MetricExpression({
  values,
  sources,
  error,
  onChange,
}: {
  values: MetricEditorValues;
  sources: MetricDefinitionSources;
  error: string | null;
  onChange: (patch: Partial<MetricEditorValues>) => void;
}) {
  const names = sources.metrics.map((item) => item.bizName);
  const binary = readBinaryExpr(values.expr, names);
  // 复合指标绝大多数就是两个指标一个运算（毛利 = 收入 − 成本，毛利率 = 毛利 ÷ 收入）。
  // 读不回二元式的（CASE WHEN、带常数）才落到表达式。
  const [advanced, setAdvanced] = useState(() => binary === null && values.expr.trim() !== '');
  // 和条件行同一个道理:表达式是唯一权威,但「只选了一边」没法写成表达式。纯从 expr
  // 反推的话,选中左边会把右边自动填成某个指标——实机出现了「销售金额 − 销售金额」。
  const [draft, setDraft] = useState(() => binary ?? { left: '', operator: '-' as BinaryOperator, right: '' });
  const label = (bizName: string) =>
    sources.metrics.find((item) => item.bizName === bizName)?.name ?? bizName;
  const pick = (patch: Partial<typeof draft>) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    // 两边都选齐才写得出表达式;没选齐就让它保持为空,保存按钮自然是禁用的。
    onChange({ expr: next.left && next.right ? writeBinaryExpr(next) : '' });
  };

  if (sources.metrics.length === 0) {
    return (
      <span className="text-[11px] text-slate-400">
        该模型还没有其它指标可以组合——先在字段上建几个原子指标。
      </span>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {advanced ? (
        <ExpressionArea
          value={values.expr}
          tokens={sources.metrics.map((item) => ({
            key: item.id,
            label: item.name,
            token: item.bizName,
          }))}
          emptyHint="该模型还没有其它指标"
          error={error}
          onChange={(expr) => onChange({ expr })}
        />
      ) : (
        <div className="flex items-center gap-2">
          <Select className="h-8 flex-1" value={draft.left} onChange={(e) => pick({ left: e.target.value })}>
            <option value="">选择指标</option>
            {names.map((name) => (
              <option key={name} value={name}>
                {label(name)}
              </option>
            ))}
          </Select>
          <Select
            className="h-8 w-[104px]"
            value={draft.operator}
            onChange={(e) => pick({ operator: e.target.value as BinaryOperator })}
          >
            {(Object.keys(OPERATOR_LABEL) as BinaryOperator[]).map((op) => (
              <option key={op} value={op}>
                {OPERATOR_LABEL[op]}
              </option>
            ))}
          </Select>
          <Select className="h-8 flex-1" value={draft.right} onChange={(e) => pick({ right: e.target.value })}>
            <option value="">选择指标</option>
            {names.map((name) => (
              <option key={name} value={name}>
                {label(name)}
              </option>
            ))}
          </Select>
        </div>
      )}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            if (advanced) setDraft(readBinaryExpr(values.expr, names) ?? draft);
            setAdvanced((prev) => !prev);
          }}
        >
          {advanced ? '改回两个指标相算' : '改用表达式'}
        </Button>
        <span className="text-[11px] text-slate-400">
          {advanced
            ? '可用四则与 CASE WHEN 等标量写法，不能写聚合函数；除法自动防除零'
            : '要写 CASE WHEN 或带常数时改用表达式'}
        </span>
      </div>
    </div>
  );
}

function ConditionsEditor({
  filterSql,
  columns,
  preferredColumn,
  valuesByColumn,
  onChange,
}: {
  filterSql: string;
  columns: Array<{ column: string; name: string }>;
  /** 新加一行时预选哪一列。默认给第一列会预选出主标识——过滤主键没有意义。 */
  preferredColumn: string;
  valuesByColumn: Map<string, string[]>;
  onChange: (filterSql: string) => void;
}) {
  const parsed = parseConditions(filterSql);
  // 条件行必须有自己的草稿状态：serializeConditions 会滤掉取值还没填的行（那种行
  // 序列化不出合法 SQL），纯从 filterSql 反解的话，新加的空行写下去是 null、读回来
  // 还是空数组——「添加条件」点了没有任何反应就是这么来的。
  const [draft, setDraft] = useState<FilterCondition[]>(() => parsed ?? []);
  const write = (next: FilterCondition[]) => {
    setDraft(next);
    onChange(serializeConditions(next) ?? '');
  };

  if (parsed === null) {
    // OR、函数、列比列……选择器画不出来。转只读原文，而不是把它改写成能画的形状。
    return (
      <Field label="只统计满足条件的行" hint="这条条件选择器表达不了，保持原样；清空可改用选择器">
        <div className="flex items-center gap-2">
          <span className="flex-1 truncate rounded-md border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-[12px] text-slate-600">
            {filterSql}
          </span>
          <Button size="sm" variant="ghost" onClick={() => onChange('')}>
            清空
          </Button>
        </div>
      </Field>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-slate-600">只统计满足条件的行</span>
        <span className="text-[11px] text-slate-400">留空 = 全部行</span>
      </div>
      {draft.map((condition, index) => {
        const options = valuesByColumn.get(condition.column) ?? [];
        const patch = (next: Partial<FilterCondition>) =>
          write(draft.map((item, i) => (i === index ? { ...item, ...next } : item)));
        return (
          <div key={index} className="flex items-center gap-2">
            <Select
              className="h-8 flex-1"
              value={condition.column}
              onChange={(e) => patch({ column: e.target.value, value: '' })}
            >
              {columns.map((item) => (
                <option key={item.column} value={item.column}>
                  {item.name || item.column}
                </option>
              ))}
            </Select>
            <Select
              className="h-8 w-[88px]"
              value={condition.operator}
              onChange={(e) => patch({ operator: e.target.value as FilterOp })}
            >
              {FILTER_OPS.map((op) => (
                <option key={op} value={op}>
                  {FILTER_OP_LABEL[op]}
                </option>
              ))}
            </Select>
            {options.length > 0 && condition.operator !== 'IN' ? (
              <Select
                className="h-8 flex-1"
                value={condition.value}
                onChange={(e) => patch({ value: e.target.value })}
              >
                <option value="">选择取值</option>
                {options.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                className="h-8 flex-1"
                value={condition.value}
                placeholder={condition.operator === 'IN' ? '多个取值用「，」分隔' : '取值'}
                onChange={(e) => patch({ value: e.target.value })}
              />
            )}
            <button
              type="button"
              aria-label="删除条件"
              onClick={() => write(draft.filter((_, i) => i !== index))}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus className="h-3 w-3" />}
          disabled={columns.length === 0}
          onClick={() =>
            write([
              ...draft,
              { column: preferredColumn || columns[0]?.column || '', operator: '=', value: '' },
            ])
          }
        >
          添加条件
        </Button>
        <span className="text-[11px] text-slate-400">取值从已发布的维度值里选，不写 SQL</span>
      </div>
    </div>
  );
}

export function MetricEditor({
  metric,
  modelId,
  spec,
  sources,
  saving,
  aliasSuggest,
  onSave,
  onDelete,
  onClose,
  seedColumn,
  seedShape,
}: {
  /** ``null`` = 新建。 */
  metric: AnalyticsCatalogMetric | null;
  modelId: string;
  spec: AnalyticsSemanticSpec;
  sources: MetricDefinitionSources;
  saving: boolean;
  /** 别名「建议」按钮(需要 revision 上下文,由外层注入)。 */
  aliasSuggest?: (current: string, apply: (merged: string) => void) => React.ReactNode;
  onSave: (values: MetricEditorValues) => void;
  onDelete?: () => void;
  onClose: () => void;
  /** 从某个字段行的「+ 指标」进来时预填的列。 */
  seedColumn?: string;
  /** 从哪个入口进来的：字段行 = 对一列做统计，复合指标分组 = 由其它指标计算。 */
  seedShape?: 'column' | 'metric';
}) {
  const creating = metric === null;
  const modelFields = useMemo(
    () => spec.fields.filter((f) => f.model_id === modelId),
    [spec.fields, modelId],
  );
  const columns = useMemo(
    () => modelFields.map((f) => ({ column: f.column, name: f.name, dataType: f.data_type })),
    [modelFields],
  );
  const [form, setForm] = useState<MetricEditorValues>(() =>
    metricEditorInitial(metric, {
      column: seedColumn ?? (seedShape === 'metric' ? undefined : defaultColumn(columns, sources)),
      shape: seedShape,
    }),
  );
  const [showMore, setShowMore] = useState(false);
  // 下钻只能选同模型的维度:跨模型下钻会让指标落到无法到达的粒度上。
  const dimensionOptions = useMemo(
    () => spec.dimensions.filter((d) => d.model_id === modelId),
    [spec.dimensions, modelId],
  );
  // 已发布的维度值:限定的取值从这里选,而不是让人凭记忆打字。
  const valuesByColumn = useMemo(() => {
    const fieldById = new Map(modelFields.map((f) => [f.id, f.column]));
    const columnByDimension = new Map(
      dimensionOptions.map((d) => [d.id, fieldById.get(d.field_id ?? '') ?? '']),
    );
    const map = new Map<string, string[]>();
    (spec.dimension_values ?? []).forEach((item) => {
      const column = columnByDimension.get(item.dimension_id);
      if (!column) return;
      map.set(column, [...(map.get(column) ?? []), String(item.value)]);
    });
    return map;
  }, [modelFields, dimensionOptions, spec.dimension_values]);
  // 限定是「只统计满足条件的行」,默认该落在一个可分组的维度上,而不是主标识。
  const preferredFilterColumn = useMemo(
    () =>
      modelFields.find((f) => f.kind === 'dimension')?.column ??
      modelFields.find((f) => f.kind !== 'identifier')?.column ??
      '',
    [modelFields],
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
  const shape = readShape(form.metricDefineType, form.expr, sources);
  // 单位是列的属性(这列存的是元还是万元),原子指标默认继承来源列;
  // 复合指标没有来源列,用展示格式表达(百分比/小数)。
  const inheritedUnit =
    shape?.shape === 'column'
      ? (modelFields.find((f) => f.column === shape.column)?.unit ?? null)
      : null;
  const bizNameTaken =
    creating &&
    sources.metrics.some(
      (item) => item.bizName.toLowerCase() === form.bizName.trim().toLowerCase(),
    );
  // 还没填不是错误——保存按钮本来就是禁用的。只在写错或撞名时报红。
  const bizNameError =
    creating && form.bizName.trim()
      ? !BIZ_NAME_RE.test(form.bizName.trim())
        ? '只能用字母、数字与下划线，且不以数字开头'
        : bizNameTaken
          ? '这个英文标识已经被占用'
          : null
      : null;
  const bizNameMissing = creating && !form.bizName.trim();

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-3">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            <span className="text-[13px] font-medium text-slate-700">口径定义</span>
            <Badge tone={form.metricDefineType === 'METRIC' ? 'slate' : 'sky'} variant="outline">
              {form.metricDefineType === 'METRIC' ? '复合指标' : '原子指标'}
            </Badge>
          </span>
          <span className="text-[11px] text-amber-700">改动会直接改变问数结果</span>
        </div>
        <ShapeEditor
          values={form}
          columns={columns}
          sources={sources}
          error={creating && !form.expr.trim() ? null : definition.error}
          onChange={(patch) => setForm((prev) => ({ ...prev, ...patch }))}
        />
        {form.metricDefineType === 'METRIC' ? (
          <span className="text-[11px] text-slate-400">
            复合指标没有「只统计满足条件的行」和「按哪个时间统计」——两者都由它引用的原子指标决定。
          </span>
        ) : (
          <ConditionsEditor
            filterSql={form.filterSql}
            columns={columns}
            preferredColumn={preferredFilterColumn}
            valuesByColumn={valuesByColumn}
            onChange={(filterSql) => set('filterSql', filterSql)}
          />
        )}
      </div>

      <Field label="业务名称">
        <Input value={form.name} onChange={(e) => set('name', e.target.value)} />
      </Field>
      {creating && (
        <Field
          label="英文标识"
          hint="其它指标在口径表达式里引用你时用它;建好之后不再改,改了会打断引用"
        >
          <Input
            className="font-mono"
            value={form.bizName}
            onChange={(e) => set('bizName', e.target.value)}
          />
          {bizNameError && <div className="mt-1 text-[11px] text-red-600">{bizNameError}</div>}
        </Field>
      )}
      <Field label="别名" hint="用「，」分隔;问数时用户可能说出的其它叫法">
        <div className="flex gap-2">
          <Input value={form.aliases} onChange={(e) => set('aliases', e.target.value)} />
          {aliasSuggest?.(form.aliases, (merged) => set('aliases', merged))}
        </div>
      </Field>
      <Field label="口径说明" hint="会进入模型提示,写清口径(如「已扣退款」)">
        <Textarea
          rows={2}
          value={form.description}
          onChange={(e) => set('description', e.target.value)}
        />
      </Field>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
        {form.metricDefineType !== 'METRIC' && (
          <Field
            label="单位"
            hint={
              inheritedUnit
                ? '单位是列的属性，继承自来源列；要改去改那一列'
                : '来源列还没声明单位。用户说「超过 2 万」时系统无从换算，建议去字段上补'
            }
          >
            <Input value={inheritedUnit ?? '未声明'} disabled />
          </Field>
        )}
        {form.metricDefineType !== 'METRIC' && timeDimensions.length > 1 && (
          <Field
            label="按哪个时间统计"
            hint="问「本月」时按哪个时间列统计。本模型有多个时间列，不指定就可能统计错列"
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
      </div>

      <button
        type="button"
        onClick={() => setShowMore((prev) => !prev)}
        className="flex w-fit items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700"
      >
        <ChevronRight className={cx('h-3 w-3 transition-transform', showMore && 'rotate-90')} />
        更多（展示格式、小数位、敏感度、业务分类）
      </button>
      {showMore && (
        <div className="flex flex-col gap-3 rounded-md border border-slate-200 px-3 py-3">
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
        </div>
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
            disabled={
              !form.name.trim() ||
              definition.error !== null ||
              bizNameError !== null ||
              bizNameMissing
            }
            onClick={() => onSave(form)}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  );
}
