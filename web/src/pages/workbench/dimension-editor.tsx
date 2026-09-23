import { useMemo, useState } from 'react';
import type { AnalyticsCatalogDimension } from '@analytics/api/types';
import { Button, Field, Input, Select, Textarea } from '@analytics/components/ui';
import { ExpressionArea } from './expression-field';
import { readColumnExpr, writeColumnExpr } from './expression-builder';
import {
  DIMENSION_KIND_LABEL,
  type DimensionEditorValues,
  type DimensionKind,
  GRANULARITY_LABEL,
  TIME_GRANULARITIES,
  checkDimensionExpression,
  dimensionEditorInitial,
} from './dimension-definition';


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
  // 实测 63 个维度表达式全是裸列引用。默认就该是「选一列」,而不是让人手打 "门店名称"
  // ——还得自己知道要加双引号。读不回单列的（CASE WHEN 分箱这类）才落到表达式。
  const [advanced, setAdvanced] = useState(
    () => readColumnExpr(dimensionEditorInitial(dimension).expr, columns) === null,
  );
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
      <div className="rounded-md border border-[var(--kf-border-secondary)] bg-[rgb(var(--kf-fill-alter-rgb))] px-3 py-3">
        <div className="mb-2 text-sm font-medium text-[var(--kf-text)]">取值来源</div>
        {advanced ? (
          <Field label="表达式" hint="引用本模型的物理列；聚合属于指标，这里不能写 SUM/COUNT">
            <ExpressionArea
              value={form.expr}
              tokens={columns.map((column) => ({
                key: column,
                label: column,
                token: writeColumnExpr(column),
              }))}
              emptyHint="该模型还没有字段"
              error={check.error}
              onChange={(expr) => set('expr', expr)}
            />
          </Field>
        ) : (
          <Field label="取自哪一列">
            <Select
              value={readColumnExpr(form.expr, columns) ?? ''}
              onChange={(e) => set('expr', writeColumnExpr(e.target.value))}
            >
              {readColumnExpr(form.expr, columns) === null && <option value="">选择一列</option>}
              {columns.map((column) => (
                <option key={column} value={column}>
                  {column}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <div className="mt-2 flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setAdvanced((prev) => !prev)}
          >
            {advanced ? '改回选一列' : '改用表达式'}
          </Button>
          <span className="text-xs text-[var(--kf-text-tertiary)]">
            {advanced
              ? '分箱、截取、拼接这类才需要表达式；回到「选一列」会清掉现在的写法'
              : '要按区间分箱或截取一段时改用表达式'}
          </span>
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
        <div className="text-xs text-[var(--kf-text-secondary)]">
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
