import type {
  AnalyticsDataset,
  AnalyticsQueryScopeCompilationDiagnostic,
  AnalyticsSemanticCatalog,
  AnalyticsSemanticContextEntry,
  AnalyticsSemanticSpec,
} from '@analytics/api/types';

export type WorkbenchStep = 'tables' | 'catalog' | 'publish' | 'feedback';

/** Preserve old deep links without restoring legacy editable-topic UI. */
export function normalizeWorkbenchStep(value: string | null): WorkbenchStep {
  if (value === 'feedback') return 'feedback';
  if (value === 'publish') return 'publish';
  if (value === 'catalog' || value === 'canvas' || value === 'ai' || value === 'topics') {
    return 'catalog';
  }
  return 'tables';
}

export interface CatalogModelRow {
  id: string;
  name: string;
  physicalName: string;
  description: string;
  metricNames: string[];
  dimensionNames: string[];
}

export type CatalogResourceKind =
  | 'models'
  | 'fields'
  | 'dimensions'
  | 'measures'
  | 'metrics'
  | 'terms'
  | 'dimensionValues';

export interface CatalogResourceRow {
  id: string;
  title: string;
  subtitle: string;
  description: string;
  aliases: string[];
  details: Array<{ label: string; value: string }>;
  /** Original API DTO, retained verbatim for lossless review. */
  raw: unknown;
}

export type CatalogInventory = Record<CatalogResourceKind, CatalogResourceRow[]>;

export interface QueryScopeDiagnostic {
  id: string;
  name: string;
  rootName: string;
  modelNames: string[];
  metricNames: string[];
  dimensionNames: string[];
  defaultCountName: string | null;
  pathLabels: string[];
  hasRoute: boolean;
}

export interface ProposalScopeRow {
  datasetId: string;
  datasetName: string;
  rootName: string;
  modelNames: string[];
  metricNames: string[];
  dimensionNames: string[];
  defaultCountName: string | null;
  pathLabels: string[];
  canonicalNames: Array<[string, string]>;
  exclusions: Array<[string, string]>;
}

/** A complete, deterministic projection of the public semantic catalog. */
export function buildCatalogModelRows(spec: AnalyticsSemanticSpec): CatalogModelRow[] {
  const metricNamesByModel = new Map<string, string[]>();
  const dimensionNamesByModel = new Map<string, string[]>();
  spec.metrics.forEach((metric) => {
    const names = metricNamesByModel.get(metric.model_id) ?? [];
    names.push(metric.name);
    metricNamesByModel.set(metric.model_id, names);
  });
  spec.dimensions.forEach((dimension) => {
    if (dimension.metric_time_axis) return;
    const names = dimensionNamesByModel.get(dimension.model_id) ?? [];
    names.push(dimension.name);
    dimensionNamesByModel.set(dimension.model_id, names);
  });
  return spec.models.map((model) => ({
    id: model.id,
    name: model.name,
    physicalName: [model.schema_name, model.table].filter(Boolean).join('.') || 'SQL 模型',
    description: model.description ?? '',
    metricNames: metricNamesByModel.get(model.id) ?? [],
    dimensionNames: dimensionNamesByModel.get(model.id) ?? [],
  }));
}

/**
 * Lossless user-facing inventory of every governed Catalog resource family.
 * Relations remain on the graph, where their direction/cardinality is easier
 * to understand than in a flat list.
 */
export function buildCatalogInventory(
  spec: AnalyticsSemanticSpec,
  catalog: AnalyticsSemanticCatalog,
): CatalogInventory {
  const orderedIds = (...groups: readonly (readonly string[])[]) => [...new Set(groups.flat())];
  const specModels = new Map(spec.models.map((model) => [model.id, model]));
  const catalogModels = new Map(catalog.models.map((model) => [model.id, model]));
  const modelIds = orderedIds(catalog.models.map((model) => model.id), spec.models.map((model) => model.id));
  const modelNames = new Map(
    modelIds.map((id) => {
      const normalized = specModels.get(id);
      const source = catalogModels.get(id);
      return [id, normalized?.name ?? source?.bizName ?? source?.name ?? id];
    }),
  );
  const specDimensions = new Map(spec.dimensions.map((dimension) => [dimension.id, dimension]));
  const catalogDimensions = new Map(catalog.dimensions.map((dimension) => [dimension.id, dimension]));
  const dimensionIds = orderedIds(
    catalog.dimensions.map((dimension) => dimension.id),
    spec.dimensions.map((dimension) => dimension.id),
  );
  const dimensionNames = new Map(
    dimensionIds.map((id) => {
      const normalized = specDimensions.get(id);
      const source = catalogDimensions.get(id);
      return [id, normalized?.name ?? source?.bizName ?? source?.name ?? id];
    }),
  );
  const specMetrics = new Map(spec.metrics.map((metric) => [metric.id, metric]));
  const catalogMetrics = new Map(catalog.metrics.map((metric) => [metric.id, metric]));
  const metricIds = orderedIds(catalog.metrics.map((metric) => metric.id), spec.metrics.map((metric) => metric.id));
  const specTerms = new Map((spec.terms ?? []).map((term) => [term.id, term]));
  const catalogTerms = new Map(catalog.terms.map((term) => [term.id, term]));
  const termIds = orderedIds(catalog.terms.map((term) => term.id), (spec.terms ?? []).map((term) => term.id));
  const specDimensionValues = new Map((spec.dimension_values ?? []).map((value) => [value.id, value]));
  const catalogDimensionValues = new Map(catalog.dimensionValues.map((value) => [value.id, value]));
  const dimensionValueIds = orderedIds(
    catalog.dimensionValues.map((value) => value.id),
    (spec.dimension_values ?? []).map((value) => value.id),
  );
  const datasetNames = new Map([
    ...(catalog.dataSets ?? []).map((dataset) => [dataset.id, dataset.bizName || dataset.name] as const),
    ...spec.datasets.map((dataset) => [dataset.id, dataset.name] as const),
  ]);
  const fieldCountByModel = new Map<string, number>();
  spec.fields.forEach((field) => {
    fieldCountByModel.set(field.model_id, (fieldCountByModel.get(field.model_id) ?? 0) + 1);
  });
  const detail = (...items: Array<[string, string | number | null | undefined]>) =>
    items
      .filter((item): item is [string, string | number] => item[1] !== null && item[1] !== undefined && item[1] !== '')
      .map(([label, value]) => ({ label, value: String(value) }));

  return {
    models: modelIds.map((id) => {
      const model = specModels.get(id);
      const source = catalogModels.get(id);
      return {
        id,
        title: model?.name ?? source?.bizName ?? source?.name ?? id,
        subtitle: model?.biz_name || source?.bizName || id,
        description: model?.description || source?.description || '',
        aliases: model?.aliases ?? (source?.alias ? [source.alias] : []),
        raw: source ?? model,
        details: detail(
          ['查询类型', source?.modelDetail.queryType ?? model?.query_type],
          ['物理对象', [model?.schema_name, model?.table].filter(Boolean).join('.') || model?.sql_query || source?.modelDetail.tableQuery || source?.modelDetail.sqlQuery],
          ['字段', fieldCountByModel.get(id) ?? 0],
          ['Measure', source?.modelDetail.measures.length ?? 0],
        ),
      };
    }),
    fields: spec.fields.map((field) => ({
      id: field.id,
      title: field.name,
      subtitle: modelNames.get(field.model_id) ?? field.model_id,
      description: field.description,
      aliases: [],
      raw: field,
      details: detail(
        ['物理列', field.column],
        ['数据类型', field.data_type],
        ['字段角色', field.kind],
        ['标识类型', field.identifier_type],
        ['默认聚合', field.default_aggregation],
        ['单位', field.unit],
      ),
    })),
    dimensions: dimensionIds.map((id) => {
      const dimension = specDimensions.get(id);
      const source = catalogDimensions.get(id);
      const modelId = dimension?.model_id ?? source?.modelId ?? '';
      return {
        id,
        title: dimension?.name ?? source?.bizName ?? source?.name ?? id,
        subtitle: modelNames.get(modelId) ?? modelId,
        description: dimension?.description ?? source?.description ?? '',
        aliases: dimension?.aliases ?? (source?.alias ? [source.alias] : []),
        raw: source ?? dimension,
        details: detail(
          ['语义类型', dimension?.semantic_type ?? source?.semanticType],
          ['表达式', dimension?.expression ?? source?.expr],
          ['字段 ID', dimension?.field_id],
          ['指标时间轴', dimension?.metric_time_axis ? '是' : null],
          ['数据类型', source?.dataType],
        ),
      };
    }),
    measures: catalog.models.flatMap((model) =>
      model.modelDetail.measures.map((measure) => ({
        id: `measure:${model.id}:${measure.name}`,
        title: measure.bizName || measure.name,
        subtitle: modelNames.get(model.id) ?? model.name,
        description: measure.constraint ?? '',
        aliases: measure.alias ? [measure.alias] : [],
        raw: measure,
        details: detail(
          ['技术名', measure.name],
          ['聚合', measure.agg],
          ['表达式', measure.expr],
          ['单位', measure.unit],
          ['生成指标', measure.isCreateMetric ? '是' : '否'],
        ),
      })),
    ),
    metrics: metricIds.map((id) => {
      const metric = specMetrics.get(id);
      const source = catalogMetrics.get(id);
      const modelId = metric?.model_id ?? source?.modelId ?? '';
      return {
        id,
        title: metric?.name ?? source?.bizName ?? source?.name ?? id,
        subtitle: modelNames.get(modelId) ?? modelId,
        description: metric?.description ?? source?.description ?? '',
        aliases: metric?.aliases ?? (source?.alias ? [source.alias] : []),
        raw: source ?? metric,
        details: detail(
          ['类型', metric?.kind ?? source?.metricDefineType],
          ['聚合', metric?.aggregation],
          ['公式', metric?.formula],
          ['字段 ID', metric?.field_id],
          ['单位', metric?.unit],
          ['格式', metric?.format ?? source?.dataFormatType],
          ['聚合时间轴', metric?.agg_time_dimension_id ?? source?.aggTimeDimensionId],
        ),
      };
    }),
    terms: termIds.map((id) => {
      const term = specTerms.get(id) ?? catalogTerms.get(id)!;
      const source = catalogTerms.get(id);
      return {
        id,
        title: term.name,
        subtitle: term.dataset_ids.map((datasetId) => datasetNames.get(datasetId) ?? datasetId).join('、') || '项目级',
        description: term.description,
        aliases: term.aliases,
        raw: source ?? term,
        details: detail(
          ['绑定指标', term.metric_ids.length],
          ['绑定维度', term.dimension_ids.length],
          ['兼容作用域', term.dataset_ids.length],
        ),
      };
    }),
    dimensionValues: dimensionValueIds.map((id) => {
      const value = specDimensionValues.get(id) ?? catalogDimensionValues.get(id)!;
      const source = catalogDimensionValues.get(id);
      return {
        id,
        title: value.display_name,
        subtitle: dimensionNames.get(value.dimension_id) ?? value.dimension_id,
        description: '',
        aliases: value.aliases,
        raw: source ?? value,
        details: detail(
          ['物理值', typeof value.value === 'string' ? value.value : JSON.stringify(value.value)],
          ['状态', value.enabled ? '启用' : '停用'],
        ),
      };
    }),
  };
}

/** Pretty, stable JSON used by the lossless per-resource detail view. */
export function serializeCatalogResource(row: Pick<CatalogResourceRow, 'raw'>): string {
  return JSON.stringify(row.raw, null, 2) ?? 'null';
}

export function sliceCatalogRows<T>(
  rows: readonly T[],
  limit: number,
): { visible: readonly T[]; remaining: number; nextLimit: number } {
  const safeLimit = Math.max(0, Math.floor(limit));
  const visible = rows.slice(0, safeLimit);
  const remaining = Math.max(0, rows.length - visible.length);
  return {
    visible,
    remaining,
    nextLimit: Math.min(rows.length, safeLimit + 100),
  };
}

/**
 * Render the legacy Dataset/AnalysisTopicRoute wire format as compiler-owned
 * QueryScope diagnostics. No value returned here is an editable resource.
 */
export function buildScopeDiagnostics(spec: AnalyticsSemanticSpec): QueryScopeDiagnostic[] {
  const modelNames = new Map(spec.models.map((model) => [model.id, model.name]));
  const metricNames = new Map(spec.metrics.map((metric) => [metric.id, metric.name]));
  const dimensionNames = new Map(spec.dimensions.map((dimension) => [dimension.id, dimension.name]));
  const routes = new Map((spec.analysis_topic_routes ?? []).map((route) => [route.dataset_id, route]));

  return spec.datasets.map((dataset) => {
    const route = routes.get(dataset.id);
    const rootModelId = route?.root_model_id ?? dataset.model_ids[0] ?? '';
    const rootName = modelNames.get(rootModelId) ?? (rootModelId || '未知');
    return {
      id: dataset.id,
      name: dataset.name,
      rootName,
      modelNames: dataset.model_ids.map((id) => modelNames.get(id) ?? id),
      metricNames: dataset.metric_ids.map((id) => metricNames.get(id) ?? id),
      dimensionNames: dataset.dimension_ids.map((id) => dimensionNames.get(id) ?? id),
      defaultCountName: route?.default_count_metric_id
        ? metricNames.get(route.default_count_metric_id) ?? route.default_count_metric_id
        : null,
      pathLabels: (route?.paths ?? []).map((path) => {
        const targetName = modelNames.get(path.target_model_id) ?? path.target_model_id;
        const qualifier = [path.prefix, path.relation_ids.join(' → ')].filter(Boolean).join('；');
        return `${rootName} → ${targetName}${qualifier ? `（${qualifier}）` : ''}`;
      }),
      hasRoute: Boolean(route),
    };
  });
}

/** Consume the versioned backend compiler evidence verbatim, adding labels only. */
export function buildProposalScopeRows(
  spec: AnalyticsSemanticSpec,
  datasets: readonly AnalyticsDataset[],
  diagnostics: readonly AnalyticsQueryScopeCompilationDiagnostic[],
): ProposalScopeRow[] {
  const models = new Map(spec.models.map((model) => [model.id, model.name]));
  const metrics = new Map(spec.metrics.map((metric) => [metric.id, metric.name]));
  const dimensions = new Map(spec.dimensions.map((dimension) => [dimension.id, dimension.name]));
  const datasetNames = new Map(datasets.map((dataset) => [dataset.id, dataset.name]));
  const relations = new Map(
    spec.relations.map((relation) => [
      relation.id,
      `${models.get(relation.left_model_id) ?? relation.left_model_id} → ${models.get(relation.right_model_id) ?? relation.right_model_id}`,
    ]),
  );
  const elementNames = new Map([
    ...models,
    ...metrics,
    ...dimensions,
  ]);
  return diagnostics.map((item) => ({
    datasetId: item.dataset_id,
    datasetName: datasetNames.get(item.dataset_id) ?? item.dataset_id,
    rootName: models.get(item.root_model_id) ?? item.root_model_id,
    modelNames: item.model_ids.map((id) => models.get(id) ?? id),
    metricNames: item.metric_ids.map((id) => metrics.get(id) ?? id),
    dimensionNames: item.dimension_ids.map((id) => dimensions.get(id) ?? id),
    defaultCountName: item.default_count_metric_id
      ? metrics.get(item.default_count_metric_id) ?? item.default_count_metric_id
      : null,
    pathLabels: item.path_relation_ids.map((path) =>
      path.map((relationId) => relations.get(relationId) ?? relationId).join(' / '),
    ),
    canonicalNames: Object.entries(item.canonical_names).sort(([left], [right]) => left.localeCompare(right)),
    exclusions: item.exclusions.map((entry) => [
      elementNames.get(entry.element_id) ?? entry.element_id,
      entry.reason_code,
    ]),
  }));
}

/** SemanticContext review must be lossless: never hide entries by target/source. */
export function allSemanticContextEntries(
  entries: readonly AnalyticsSemanticContextEntry[] | undefined,
): AnalyticsSemanticContextEntry[] {
  return [...(entries ?? [])];
}

export function visibleSemanticContext(spec: AnalyticsSemanticSpec): AnalyticsSemanticContextEntry[] {
  return allSemanticContextEntries(spec.semantic_context);
}
