import type {
  AnalyticsCatalogMeasure,
  AnalyticsCatalogMetric,
  AnalyticsCatalogMetricDefineType,
} from '@analytics/api/types';
import { parseExpression, resolveAgainst } from './semantic-expression';

/**
 * 指标口径表达式的客户端解析与校验。
 *
 * 服务端 `_validate_columns` 要求「选中的来源」与「表达式实际引用」完全相等,
 * 所以来源不做成第二个可编辑控件——用户只写表达式,来源一律从表达式反推。
 * 两处各自维护必然漂移,而漂移的报错(selected fields must exactly match
 * expression references)对用户是不可理解的。
 *
 * 表达式解析本身在 semantic-expression.ts,与维度表达式共用。
 */

export interface MetricDefinitionSources {
  /** 本模型的受治理度量。 */
  measures: AnalyticsCatalogMeasure[];
  /** 本模型的受治理物理列。 */
  fieldColumns: string[];
  /** 可被组合的其它指标。 */
  metrics: Array<Pick<AnalyticsCatalogMetric, 'id' | 'bizName' | 'name'>>;
}

export interface DefinitionCheck {
  /** 阻断保存的问题;为空表示可以提交。 */
  error: string | null;
  /** 表达式实际引用到的来源名(规范化为目录里的写法)。 */
  resolved: string[];
}

function availableNames(
  type: AnalyticsCatalogMetricDefineType,
  sources: MetricDefinitionSources,
): string[] {
  if (type === 'MEASURE') return sources.measures.map((m) => m.bizName);
  if (type === 'FIELD') return sources.fieldColumns;
  return sources.metrics.map((m) => m.bizName);
}

export const DEFINE_TYPE_RULE: Record<AnalyticsCatalogMetricDefineType, string> = {
  MEASURE: '引用本模型度量的英文名(bizName);度量自带聚合,表达式里不能再写 SUM/COUNT 等聚合函数',
  FIELD: '引用本模型的物理列,且必须带聚合函数',
  // 翻译层已对齐建模接受集(token 替换 + sqlglot,两条路径同一实现):四则、
  // CASE WHEN 等标量写法都可用;聚合/窗口/子查询仍由建模校验器拒绝。
  METRIC: '引用其它指标的英文名组合成派生指标;可用四则与 CASE WHEN 等标量写法,不能写聚合函数;除法自动防除零',
};

/** 每种定义方式的可抄写法。静态精选:动态拼当前模型列名容易拼出 SUM(id) 这种反面教材。 */
export const DEFINE_TYPE_EXAMPLES: Record<AnalyticsCatalogMetricDefineType, string[]> = {
  FIELD: ['SUM(net_amount)', 'SUM(refund_amount) / SUM(net_amount)', 'COUNT(DISTINCT customer_id)'],
  MEASURE: ['net_amount', 'net_amount - refund_amount'],
  METRIC: [
    'net_revenue - refund_amount',
    'gross_profit / net_revenue',
    'CASE WHEN net_revenue > 0 THEN gross_profit / net_revenue ELSE 0 END',
  ],
};

/** 校验表达式并解析出来源;错误信息面向写表达式的人。 */
export function checkDefinition(
  type: AnalyticsCatalogMetricDefineType,
  expr: string,
  sources: MetricDefinitionSources,
): DefinitionCheck {
  const text = expr.trim();
  if (!text) return { error: '表达式不能为空', resolved: [] };
  const { identifiers, hasAggregate, hasQualified } = parseExpression(text);
  if (hasQualified) {
    return { error: '表达式不能用「表名.字段」限定,直接写字段名', resolved: [] };
  }
  if (type === 'MEASURE' && hasAggregate) {
    return { error: '度量已经自带聚合,表达式里不要再写 SUM/COUNT 等聚合函数', resolved: [] };
  }
  if (type === 'FIELD' && !hasAggregate) {
    return { error: '字段表达式必须带聚合函数,例如 SUM(net_amount)', resolved: [] };
  }
  const available = availableNames(type, sources);
  return resolveAgainst(identifiers, available, '表达式至少要引用一个来源');
}

export type MetricDefineParams = Pick<
  AnalyticsCatalogMetric,
  'metricDefineType' | 'metricDefineByMeasureParams' | 'metricDefineByFieldParams' | 'metricDefineByMetricParams'
>;

/**
 * 拼装 params。合同要求「有且只有一个 params 对象」
 * (catalog_contracts.MetricContract.validate_definition),所以另外两个必须显式置空。
 */
export function buildDefineParams(
  type: AnalyticsCatalogMetricDefineType,
  expr: string,
  filterSql: string,
  resolved: string[],
  sources: MetricDefinitionSources,
): MetricDefineParams {
  const base = {
    expr: expr.trim(),
    filterSql: filterSql.trim() || null,
  };
  const empty = {
    metricDefineByMeasureParams: null,
    metricDefineByFieldParams: null,
    metricDefineByMetricParams: null,
  };
  if (type === 'MEASURE') {
    const byName = new Map(sources.measures.map((m) => [m.bizName.toLowerCase(), m]));
    return {
      ...empty,
      metricDefineType: 'MEASURE',
      // 带回目录里的完整度量对象:agg/unit 这些由建模治理,不该在这里被重新发明。
      metricDefineByMeasureParams: {
        ...base,
        measures: resolved
          .map((name) => byName.get(name.toLowerCase()))
          .filter((m): m is AnalyticsCatalogMeasure => Boolean(m)),
      },
    };
  }
  if (type === 'FIELD') {
    return {
      ...empty,
      metricDefineType: 'FIELD',
      metricDefineByFieldParams: { ...base, fields: resolved.map((fieldName) => ({ fieldName })) },
    };
  }
  const byName = new Map(sources.metrics.map((m) => [m.bizName.toLowerCase(), m]));
  return {
    ...empty,
    metricDefineType: 'METRIC',
    metricDefineByMetricParams: {
      ...base,
      metrics: resolved
        .map((name) => byName.get(name.toLowerCase()))
        .filter(Boolean)
        .map((m) => ({ id: (m as { id: string }).id, bizName: (m as { bizName: string }).bizName })),
    },
  };
}

/** 读出当前生效的那份 params。 */
export function activeParams(metric: AnalyticsCatalogMetric): {
  expr: string;
  filterSql: string;
} {
  const params =
    metric.metricDefineType === 'MEASURE'
      ? metric.metricDefineByMeasureParams
      : metric.metricDefineType === 'FIELD'
        ? metric.metricDefineByFieldParams
        : metric.metricDefineByMetricParams;
  return { expr: params?.expr ?? '', filterSql: params?.filterSql ?? '' };
}
