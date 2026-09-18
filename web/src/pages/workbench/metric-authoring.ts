import type { AnalyticsCatalogMetricDefineType } from '@analytics/api/types';
import type { MetricDefinitionSources } from './metric-definition';

/**
 * 建模者面前的指标形态，与目录里的定义方式之间的换算。
 *
 * 界面上只问一句「这个指标怎么算」，两个答案：**对一列做统计** / **由其它指标计算**。
 * 目录里的 FIELD / MEASURE / METRIC 是编译器的分类——它决定 params 的形状，
 * 但要用户先理解它才能建指标，是把实现细节当成了入门考试。这里把两者对上：
 * 形态由用户选，定义方式由系统定。
 *
 * 「只统计满足条件的行」同理：存进目录的仍是 filterSql（编译器用
 * ``compile_fixed_filters`` 确定性解析成 FixedFilter，只接受列与字面量的合取），
 * 但用户不写 SQL，选维度、选比较、选已发布的取值。选择器表达不了的写法
 * （OR、函数、列比列）不悄悄改写它——读成 null，由界面转只读原文。
 */

export type MetricShape = 'column' | 'metric';

export type AggName = 'SUM' | 'COUNT' | 'COUNT_DISTINCT' | 'AVG' | 'MIN' | 'MAX';

export const AGG_LABEL: Record<AggName, string> = {
  SUM: '求和',
  COUNT: '计数',
  COUNT_DISTINCT: '去重计数',
  AVG: '平均',
  MIN: '最小值',
  MAX: '最大值',
};

export interface ColumnChoice {
  column: string;
  aggregation: AggName;
}

export type MetricShapeRead =
  | { shape: 'column'; column: string; aggregation: AggName }
  | { shape: 'metric'; column: null; aggregation: null };

const BARE_IDENTIFIER = /^[A-Za-z_一-龥][\w一-龥]*$/;

/** 选中的列上有没有一个聚合方式完全一致的受治理度量。 */
function governedMeasure(choice: ColumnChoice, sources: MetricDefinitionSources) {
  return sources.measures.find(
    (item) =>
      item.expr.trim().toLowerCase() === choice.column.trim().toLowerCase() &&
      (item.agg ?? '').toUpperCase() === choice.aggregation,
  );
}

/**
 * 形态 → 目录里的定义方式。
 *
 * 选中的列正好有同聚合的受治理度量时走 MEASURE：度量上带着 unit、别名与口径说明，
 * 那些是建模治理过的，走 FIELD 会把它们丢掉。聚合方式不一致（净额是 SUM，用户要
 * AVG）本来就不是同一个口径，走 FIELD。
 */
export function deriveDefineType(
  shape: MetricShape,
  choice: ColumnChoice | null,
  sources: MetricDefinitionSources,
): AnalyticsCatalogMetricDefineType {
  if (shape === 'metric') return 'METRIC';
  if (choice && governedMeasure(choice, sources)) return 'MEASURE';
  return 'FIELD';
}

/** 形态 → 表达式。MEASURE 写度量英文名（聚合由度量自带），FIELD 写聚合函数。 */
export function columnExpr(choice: ColumnChoice, sources: MetricDefinitionSources): string {
  const measure = governedMeasure(choice, sources);
  if (measure) return measure.bizName;
  if (choice.aggregation === 'COUNT_DISTINCT') return `COUNT(DISTINCT ${choice.column})`;
  return `${choice.aggregation}(${choice.column})`;
}

/** 从目录里的定义方式与表达式读回用户面前的形态；选择器表达不了时返回 null。 */
export function readShape(
  defineType: AnalyticsCatalogMetricDefineType,
  expr: string,
  sources: MetricDefinitionSources,
): MetricShapeRead | null {
  if (defineType === 'METRIC') return { shape: 'metric', column: null, aggregation: null };
  const text = expr.trim();
  if (defineType === 'MEASURE') {
    const measure = sources.measures.find(
      (item) => item.bizName.trim().toLowerCase() === text.toLowerCase(),
    );
    if (!measure || !BARE_IDENTIFIER.test(measure.expr.trim())) return null;
    const agg = (measure.agg ?? '').toUpperCase();
    if (!(agg in AGG_LABEL)) return null;
    return { shape: 'column', column: measure.expr.trim(), aggregation: agg as AggName };
  }
  const distinct = /^COUNT\s*\(\s*DISTINCT\s+(.+?)\s*\)$/i.exec(text);
  if (distinct && BARE_IDENTIFIER.test(distinct[1])) {
    return { shape: 'column', column: distinct[1], aggregation: 'COUNT_DISTINCT' };
  }
  const plain = /^(SUM|COUNT|AVG|MIN|MAX)\s*\(\s*(.+?)\s*\)$/i.exec(text);
  if (plain && BARE_IDENTIFIER.test(plain[2])) {
    return {
      shape: 'column',
      column: plain[2],
      aggregation: plain[1].toUpperCase() as AggName,
    };
  }
  return null;
}

// ---- 限定条件 --------------------------------------------------------------

export type FilterOp = '=' | '!=' | '>' | '>=' | '<' | '<=' | 'IN';

export const FILTER_OP_LABEL: Record<FilterOp, string> = {
  '=': '等于',
  '!=': '不等于',
  '>': '大于',
  '>=': '大于等于',
  '<': '小于',
  '<=': '小于等于',
  IN: '属于',
};

export interface FilterCondition {
  column: string;
  operator: FilterOp;
  value: string;
}

/** 按顶层 AND 切开；遇到 OR 直接判定选择器表达不了。引号内的关键字不算。 */
function splitTopLevelAnd(sql: string): string[] | null {
  const parts: string[] = [];
  let buffer = '';
  let index = 0;
  let inSingle = false;
  let inDouble = false;
  while (index < sql.length) {
    const char = sql[index];
    if (inSingle) {
      if (char === "'" && sql[index + 1] === "'") {
        buffer += "''";
        index += 2;
        continue;
      }
      if (char === "'") inSingle = false;
      buffer += char;
      index += 1;
      continue;
    }
    if (inDouble) {
      if (char === '"') inDouble = false;
      buffer += char;
      index += 1;
      continue;
    }
    if (char === "'") inSingle = true;
    if (char === '"') inDouble = true;
    if (!inSingle && !inDouble) {
      const rest = sql.slice(index);
      if (/^\s+OR\s+/i.test(rest)) return null;
      const and = /^\s+AND\s+/i.exec(rest);
      if (and) {
        parts.push(buffer);
        buffer = '';
        index += and[0].length;
        continue;
      }
    }
    buffer += char;
    index += 1;
  }
  parts.push(buffer);
  return parts;
}

function stripParens(text: string): string {
  let current = text.trim();
  while (current.startsWith('(') && current.endsWith(')')) {
    const inner = current.slice(1, -1).trim();
    // 只剥真正包住整条的那对括号。
    if (splitTopLevelAnd(inner) === null) break;
    current = inner;
  }
  return current;
}

const COLUMN = String.raw`(?:"([^"]+)"|([A-Za-z_一-龥][\w一-龥]*))`;

function unquote(literal: string): string | null {
  const text = literal.trim();
  if (/^'([^']|'')*'$/.test(text)) return text.slice(1, -1).replace(/''/g, "'");
  if (/^-?\d+(\.\d+)?$/.test(text)) return text;
  return null;
}

/**
 * filterSql → 条件行。返回 null 表示选择器表达不了这条写法（OR、函数、列比列……）。
 *
 * 空与 null 都是「没有限定」，返回空数组——那和「表达不了」是两回事，界面要分开处理。
 */
export function parseConditions(filterSql: string | null | undefined): FilterCondition[] | null {
  if (!filterSql || !filterSql.trim()) return [];
  const parts = splitTopLevelAnd(filterSql.trim());
  if (parts === null) return null;
  const conditions: FilterCondition[] = [];
  for (const raw of parts) {
    const text = stripParens(raw);
    const inMatch = new RegExp(`^${COLUMN}\\s+IN\\s*\\((.+)\\)$`, 'i').exec(text);
    if (inMatch) {
      const column = inMatch[1] ?? inMatch[2];
      const items = inMatch[3].split(',').map((item) => unquote(item));
      if (items.length === 0 || items.some((item) => item === null)) return null;
      conditions.push({ column, operator: 'IN', value: items.join('，') });
      continue;
    }
    const compare = new RegExp(`^${COLUMN}\\s*(<>|!=|>=|<=|=|>|<)\\s*(.+)$`, 'i').exec(text);
    if (!compare) return null;
    const value = unquote(compare[4]);
    if (value === null) return null;
    const operator = compare[3] === '<>' ? '!=' : (compare[3] as FilterOp);
    conditions.push({ column: compare[1] ?? compare[2], operator, value });
  }
  return conditions;
}

function literal(value: string): string {
  if (/^-?\d+(\.\d+)?$/.test(value.trim())) return value.trim();
  return `'${value.replace(/'/g, "''")}'`;
}

/** 条件行 → filterSql。没有条件时返回 null，而不是空字符串。 */
export function serializeConditions(conditions: FilterCondition[]): string | null {
  const usable = conditions.filter((item) => item.column.trim() && item.value.trim());
  if (usable.length === 0) return null;
  return usable
    .map((item) => {
      if (item.operator === 'IN') {
        const values = item.value
          .split(/[，,]/)
          .map((part) => part.trim())
          .filter(Boolean)
          .map(literal);
        return `"${item.column}" IN (${values.join(', ')})`;
      }
      return `"${item.column}" ${item.operator} ${literal(item.value)}`;
    })
    .join(' AND ');
}
