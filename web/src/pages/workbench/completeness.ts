import type { AnalyticsRevision, AnalyticsSemanticSpec } from '@analytics/api/types';

/**
 * 建模完成度:把「上下文缺失 = 静默错答」变成常驻可见。
 *
 * 语义上下文(描述、别名、时间轴声明)是问数准确率的主导变量,且缺失时不报错、
 * 只在问数时给出看起来正常的错数。全部指标从 revision.semantic_spec 本地计算,
 * 不加后端字段、不发新请求。
 */

export interface CompletenessGauge {
  key: 'descriptions' | 'aliases' | 'timeAxis';
  label: string;
  /** 已覆盖数。 */
  covered: number;
  /** 应覆盖数;0 表示该项不适用(不显示)。 */
  total: number;
  /** 缺失项的业务名,给 tooltip/跳转清单用。 */
  missing: string[];
  /** 缺失的后果,用用户的语言。 */
  consequence: string;
}

const named = (item: { name: string }) => item.name;

export function computeCompleteness(revision: AnalyticsRevision): CompletenessGauge[] {
  const spec: AnalyticsSemanticSpec = revision.semantic_spec;
  // 逻辑时间轴是编译期自动生成的,建模者既没建它也改不了它。计入完整度只会
  // 产生「缺别名」这类无法处理的噪音,还会让多时间维度计数虚高、连带更多指标
  // 被要求声明轴。
  const authored = spec.dimensions.filter((item) => !item.metric_time_axis);
  const elements = [...spec.metrics, ...authored];

  const noDescription = elements.filter((item) => !item.description?.trim());
  // 标识符列不需要业务别名:没人会用同义词问「客户ID」,它也不是用户会说出口
  // 的说法。把它们算成缺口只会让完整度常年不满,真正该补的业务名淹没在里面
  // ——实测某项目 5 条「缺失」全是主键/外键列(客户ID、订单ID、订单明细ID)。
  // 按字段的 identifier_type 判定:这些维度的 semantic_type 是 categorical,
  // 只有底层字段才带得出标识符身份。
  const identifierFieldIds = new Set(
    spec.fields.filter((field) => field.identifier_type).map((field) => field.id),
  );
  const aliasCandidates = [
    ...spec.metrics,
    ...authored.filter((item) => !identifierFieldIds.has(item.field_id)),
  ];
  const noAliases = aliasCandidates.filter((item) => (item.aliases?.length ?? 0) === 0);

  // 时间轴只在有歧义处要求声明:模型上多于一条时间维度时,未声明的指标会
  // 静默用数据集默认时间列统计——这是全链路唯一会静默给错数字的缺口。
  const timeDimensionsByModel = new Map<string, number>();
  for (const dimension of authored) {
    if (dimension.semantic_type === 'time') {
      timeDimensionsByModel.set(
        dimension.model_id,
        (timeDimensionsByModel.get(dimension.model_id) ?? 0) + 1,
      );
    }
  }
  const needsAxis = spec.metrics.filter(
    (metric) => metric.kind === 'atomic' && (timeDimensionsByModel.get(metric.model_id) ?? 0) > 1,
  );
  const noAxis = needsAxis.filter((metric) => !metric.agg_time_dimension_id);

  return [
    {
      key: 'descriptions',
      label: '业务说明',
      covered: elements.length - noDescription.length,
      total: elements.length,
      missing: noDescription.map(named),
      consequence: '缺少说明时,AI 只能从列名推断口径',
    },
    {
      key: 'aliases',
      label: '别名',
      covered: aliasCandidates.length - noAliases.length,
      total: aliasCandidates.length,
      missing: noAliases.map(named),
      consequence: '用户换个说法就可能匹配不上,问数静默落到错的元素',
    },
    {
      key: 'timeAxis',
      label: '聚合时间轴',
      covered: needsAxis.length - noAxis.length,
      total: needsAxis.length,
      missing: noAxis.map(named),
      consequence: '模型有多条时间列,未声明的指标会按数据集默认时间列统计',
    },
  ];
}
