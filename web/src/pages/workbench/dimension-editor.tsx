import { useMemo, useState } from 'react';
import type { AnalyticsCatalogDimension } from '@analytics/api/types';
import { Button, Field, Input, Select, Textarea } from '@analytics/components/ui';
import {
  DIMENSION_KIND_LABEL,
  type DimensionEditorValues,
  type DimensionKind,
  GRANULARITY_LABEL,
  TIME_GRANULARITIES,
  checkDimensionExpression,
  dimensionEditorInitial,
} from './dimension-definition';


/** 可抄写法:最常见的两类——分箱与截取。聚合属于指标,这里不出现 SUM/COUNT。 */
const DIMENSION_EXPR_EXAMPLES = [
  "CASE WHEN net_amount >= 1000 THEN '大额' ELSE '普通' END",
  'substr(order_no, 1, 4)',
];

const SENSITIVITY = [
  { value: 0, label: '0 · 普通' },
  { value: 1, label: '1 · 内部' },
  { value: 2, label: '2 · 敏感' },
  { value: 3, label: '3 · 高敏感' },
];

const KIND_HINT: Record<DimensionKind, string> = {
  categorical: '可枚举的业务属性,问数时能用来分组和过滤',
  identifier: '实体的标识列,不参与分组统计',
  time: '时间轴,决定同比环比和默认时间范围能不能算',
};

export function DimensionEditor({
  dimension,
  columns,
  saving,
  dictionary,
  aliasSuggest,
  onSave,
  onDelete,
  onClose,
}: {
  dimension: AnalyticsCatalogDimension;
  columns: string[];
  saving: boolean;
  /** 值字典区块(需要 revision 上下文,由外层注入)。 */
  dictionary?: React.ReactNode;
  /** 别名「建议」按钮(需要 revision 上下文,由外层注入)。 */
  aliasSuggest?: (current: string, apply: (merged: string) => void) => React.ReactNode;
  onSave: (values: DimensionEditorValues) => void;
  onDelete?: () => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState<DimensionEditorValues>(() => dimensionEditorInitial(dimension));
  const set = <K extends keyof DimensionEditorValues>(key: K, value: DimensionEditorValues[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));
  const check = useMemo(
    () => checkDimensionExpression(form.expr, columns),
    [form.expr, columns],
  );
  // 目录里可能存着未受治理的粒度(如 hour),编译时会被丢弃;列出来是为了不在
  // 用户没碰这个控件时把它悄悄改掉。
  const granularities = useMemo(() => {
    const known = TIME_GRANULARITIES as readonly string[];
    return known.includes(form.timeGranularity)
      ? known
      : [...known, form.timeGranularity].filter(Boolean);
  }, [form.timeGranularity]);
  const valueMapCount = dimension.dimValueMaps?.length ?? 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-3">
        <div className="mb-2 text-[13px] font-medium text-slate-700">取值来源</div>
        <Field
          label="表达式"
          tip={
            <span className="block text-[11px] text-slate-500">
              <span className="block">引用本模型的物理列;聚合属于指标,这里不能写 SUM/COUNT。</span>
              <span className="mt-1.5 block">
                示例(点击填入):
                {DIMENSION_EXPR_EXAMPLES.map((example) => (
                  <button
                    key={example}
                    type="button"
                    onClick={() => set('expr', example)}
                    className="mt-1 block rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-left font-mono text-[11px] text-slate-600 hover:border-slate-300 hover:text-slate-800"
                  >
                    {example}
                  </button>
                ))}
              </span>
            </span>
          }
        >
          <Input
            className="font-mono text-[12px]"
            value={form.expr}
            onChange={(e) => set('expr', e.target.value)}
          />
        </Field>
        {check.error && <div className="mt-2 text-[11px] text-red-600">{check.error}</div>}
        <div className="mt-2 text-[11px] text-slate-500">
          可用字段：
          {columns.length === 0 ? (
            <span className="text-slate-400">该模型还没有字段</span>
          ) : (
            columns.map((column) => (
              <button
                key={column}
                type="button"
                title="点击插入到表达式"
                onClick={() => set('expr', `${form.expr}${form.expr.trim() ? ' ' : ''}${column}`)}
                className="ml-1 rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 hover:border-slate-300"
              >
                {column}
              </button>
            ))
          )}
        </div>
      </div>
      <Field label="业务名称">
        <Input value={form.name} onChange={(e) => set('name', e.target.value)} />
      </Field>
      <Field label="别名" hint="用「，」分隔;问数时用户可能说出的其它叫法">
        <div className="flex gap-2">
          <Input value={form.aliases} onChange={(e) => set('aliases', e.target.value)} />
          {aliasSuggest?.(form.aliases, (merged) => set('aliases', merged))}
        </div>
      </Field>
      <Field label="说明" hint="会进入模型提示,写清这个维度代表什么">
        <Textarea
          rows={2}
          value={form.description}
          onChange={(e) => set('description', e.target.value)}
        />
      </Field>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        <Field label="维度类型" hint={KIND_HINT[form.kind]}>
          <Select
            value={form.kind}
            onChange={(e) => set('kind', e.target.value as DimensionKind)}
          >
            {(Object.keys(DIMENSION_KIND_LABEL) as DimensionKind[]).map((kind) => (
              <option key={kind} value={kind}>
                {DIMENSION_KIND_LABEL[kind]}
              </option>
            ))}
          </Select>
        </Field>
        {form.kind === 'time' && (
          <Field label="时间粒度" hint="决定同比环比按什么对齐">
            <Select
              value={form.timeGranularity}
              onChange={(e) => set('timeGranularity', e.target.value)}
            >
              {granularities.map((value) => (
                <option key={value} value={value}>
                  {GRANULARITY_LABEL[value] ?? `${value}（未受治理）`}
                </option>
              ))}
            </Select>
          </Field>
        )}
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
      </div>
      <Field label="默认取值" hint="用「，」分隔;用户没指定时问数默认带上这些值">
        <Input
          value={form.defaultValues}
          onChange={(e) => set('defaultValues', e.target.value)}
        />
      </Field>
      {dictionary}
      {valueMapCount > 0 && (
        <div className="text-[11px] text-slate-500">
          该维度已配置 {valueMapCount} 条维度值别名,保存时原样保留。
        </div>
      )}
      <div className="mt-1 flex items-center justify-between">
        {onDelete ? (
          <Button variant="ghost" onClick={onDelete}>
            删除维度
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
            disabled={!form.name.trim() || check.error !== null}
            onClick={() => onSave(form)}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  );
}
