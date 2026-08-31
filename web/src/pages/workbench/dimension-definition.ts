import type { AnalyticsCatalogDimension } from '@analytics/api/types';
import { type ReferenceResolution, parseExpression, resolveAgainst } from './semantic-expression';

/**
 * 维度的表单值与 DTO 之间的转换。
 *
 * 目录里维度类型由 `type` 和 `semanticType` 两个字段表达,但编译器只认三种结果:
 * `semanticType=date` 或 `type` 里含 "time" 一律算时间,`id` 算标识,其余算类别
 * (catalog_compiler._compile_dimension)。所以表单只给一个「维度类型」控件,
 * 放两个独立控件会让用户以为把时间维度的 semanticType 改成 CATEGORY 有用。
 */

export type DimensionKind = 'categorical' | 'identifier' | 'time';

export const DIMENSION_KIND_LABEL: Record<DimensionKind, string> = {
  categorical: '类别',
  identifier: '标识',
  time: '时间',
};

/** 受治理的时间粒度,与后端 TimeGranularity 一致;其余取值编译时会被丢弃。 */
export const TIME_GRANULARITIES = ['day', 'week', 'month', 'quarter', 'year'] as const;

export const GRANULARITY_LABEL: Record<string, string> = {
  day: '日',
  week: '周',
  month: '月',
  quarter: '季',
  year: '年',
};

/** 与 catalog_compiler._compile_dimension 同一套判定。 */
export function dimensionKind(dimension: AnalyticsCatalogDimension): DimensionKind {
  const semantic = (dimension.semanticType ?? '').toLowerCase();
  if (semantic === 'date' || (dimension.type ?? '').toLowerCase().includes('time')) return 'time';
  if (semantic === 'id') return 'identifier';
  return 'categorical';
}

const CANONICAL: Record<DimensionKind, { type: string; semanticType: string }> = {
  categorical: { type: 'categorical', semanticType: 'CATEGORY' },
  identifier: { type: 'categorical', semanticType: 'ID' },
  time: { type: 'time', semanticType: 'DATE' },
};

const splitList = (text: string) =>
  text.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);

/**
 * 服务端 `_quote_identifier` 里判定「整列名 vs 真表达式」的那组字符。
 * 只看 ASCII:`成立时间（年）` 的全角括号是列名的一部分,不是 SQL 语法。
 */
const EXPRESSION_CHARS = /[()+\-*/,'" ]/;

/** 维度表达式:不能带聚合/窗口函数,只能引用本模型的物理列。 */
export function checkDimensionExpression(expr: string, columns: string[]): ReferenceResolution {
  const text = expr.trim();
  if (!text) return { error: '表达式不能为空', resolved: [] };
  const noReference = '表达式至少要引用一个字段';
  // 与服务端一致:已加引号或不含运算符的,整体就是一个列名,不做词法拆分。
  if (text.length > 1 && text.startsWith('"') && text.endsWith('"')) {
    return resolveAgainst([text.slice(1, -1).replace(/""/g, '"')], columns, noReference);
  }
  if (!EXPRESSION_CHARS.test(text)) {
    return resolveAgainst([text], columns, noReference);
  }
  const { identifiers, hasAggregate, hasQualified } = parseExpression(text);
  if (hasQualified) {
    return { error: '表达式不能用「表名.字段」限定,直接写字段名', resolved: [] };
  }
  if (hasAggregate) {
    return { error: '维度表达式不能带聚合函数,聚合属于指标', resolved: [] };
  }
  return resolveAgainst(identifiers, columns, noReference);
}

export interface DimensionEditorValues {
  name: string;
  aliases: string;
  description: string;
  sensitiveLevel: number;
  kind: DimensionKind;
  timeGranularity: string;
  expr: string;
  defaultValues: string;
}

export function dimensionEditorInitial(
  dimension: AnalyticsCatalogDimension,
): DimensionEditorValues {
  return {
    name: dimension.name,
    aliases: (dimension.alias ?? '').split(/[，,]/).filter(Boolean).join('，'),
    description: dimension.description ?? '',
    sensitiveLevel: dimension.sensitiveLevel ?? 0,
    kind: dimensionKind(dimension),
    timeGranularity: dimension.typeParams?.timeGranularity ?? 'day',
    expr: dimension.expr ?? '',
    defaultValues: (dimension.defaultValues ?? []).join('，'),
  };
}

/**
 * 把表单值合回完整 DTO。
 *
 * 类型没被改动时保留原始 `type` 字符串:目录里可能存着 `partition_time` 这类
 * 取值,它在独立维度上与 `time` 等价,但改写它会让 DTO 与建模产出对不上。
 */
export function applyDimensionEditorValues(
  existing: AnalyticsCatalogDimension,
  values: DimensionEditorValues,
  columns: string[],
): AnalyticsCatalogDimension {
  const kindChanged = values.kind !== dimensionKind(existing);
  const check = checkDimensionExpression(values.expr, columns);
  return {
    ...existing,
    name: values.name.trim(),
    description: values.description.trim(),
    alias: splitList(values.aliases).join(',') || null,
    sensitiveLevel: values.sensitiveLevel,
    // 表达式非法时原样保留旧值,避免把一个编译不过的维度写进目录。
    expr: check.error ? existing.expr : values.expr.trim(),
    defaultValues: splitList(values.defaultValues),
    ...(kindChanged ? CANONICAL[values.kind] : {}),
    typeParams:
      values.kind === 'time'
        ? {
            // isPrimary 只在模型内嵌维度上被诊断消费,这里原样带回不做控件。
            isPrimary: existing.typeParams?.isPrimary ?? 'true',
            timeGranularity: values.timeGranularity,
          }
        : (existing.typeParams ?? null),
  };
}
