// Mirrors the knowflow-analytics core response contracts (snake_case specs,
// camelCase catalog DTOs). Keep in sync with src/knowflow_analytics.

export type AnalyticsFieldKind =
  | 'identifier'
  | 'dimension'
  | 'time'
  | 'measure'
  | 'field';

export interface AnalyticsProject {
  id: string;
  name: string;
  active_release_id: string | null;
  /** ISO 8601。建库起就有，M3 起才对外返回。 */
  created_at: string;
  /** 仅列表返回：最近更新的草稿版本。 */
  latest_revision_id?: string | null;
}

export interface AnalyticsDomainGovernance {
  project_id: string;
  parent_project_id: string | null;
  classifications: string[];
  lifecycle: 'initialized' | 'online' | 'offline';
  etag: number;
  updated_by: string;
  updated_at: string;
}

export interface AnalyticsModelGraphLayout {
  project_id: string;
  revision_id: string;
  etag: number;
  positions: Array<{ model_id: string; x: number; y: number }>;
  viewport: { x: number; y: number; zoom: number };
  updated_by: string | null;
  updated_at: string;
}

export interface AnalyticsSchemaCatalog {
  items: string[];
}

export interface AnalyticsTableCatalogItem {
  schema_name: string;
  name: string;
  source_type: 'table' | 'view';
  comment: string;
}

export interface AnalyticsTableCatalog {
  items: AnalyticsTableCatalogItem[];
}

export interface AnalyticsModel {
  id: string;
  name: string;
  biz_name: string | null;
  query_type: 'table_query' | 'sql_query';
  table: string | null;
  schema_name: string | null;
  sql_query?: string | null;
  sql_variables?: Array<{
    name: string;
    valueType: 'STRING' | 'NUMBER' | 'EXPR';
    defaultValues: unknown[];
  }>;
  filter_sql?: string | null;
  description: string;
  aliases: string[];
}

export interface AnalyticsField {
  id: string;
  model_id: string;
  name: string;
  column: string;
  data_type: string;
  kind: AnalyticsFieldKind;
  identifier_type: 'primary' | 'foreign' | null;
  dimension_type:
    | 'categorical'
    | 'time'
    | 'partition_time'
    | 'primary_key'
    | 'foreign_key'
    | null;
  semantic_expr: string | null;
  unit: string | null;
  default_aggregation:
    | 'sum'
    | 'count'
    | 'count_distinct'
    | 'avg'
    | 'min'
    | 'max'
    | null;
  description: string;
  aliases: string[];
  nullable: boolean;
  create_dimension: boolean;
  create_metric: boolean;
}

export interface AnalyticsRelationCondition {
  left_field_id: string;
  right_field_id: string;
}

export interface AnalyticsRelation {
  id: string;
  left_model_id: string;
  right_model_id: string;
  join_type: 'inner' | 'left' | 'right' | 'full';
  cardinality: 'one_to_one' | 'one_to_many' | 'many_to_one' | 'many_to_many';
  conditions: AnalyticsRelationCondition[];
}

export interface AnalyticsCatalogRelation {
  id: string;
  domainId?: string | null;
  fromModelId: string;
  toModelId: string;
  joinType: string;
  joinConditions: Array<{
    leftField: string;
    rightField: string;
    operator: string;
  }>;
  /**
   * Accuracy extension: the relation DTO carries no cardinality, so
   * publication requires this separately reviewed value.
   */
  knowflowCardinality?: AnalyticsRelation['cardinality'] | null;
  /**
   * Why the edge was proposed. A database constraint is a fact; a name match is
   * a suggestion. Both need the same confirmation, but the list weights them
   * differently so the stronger evidence is confirmed first.
   */
  knowflowEvidence?: 'database_foreign_key' | 'name_convention';
  knowflowRationale?: string;
}

export interface AnalyticsDimension {
  id: string;
  name: string;
  model_id: string;
  field_id: string;
  aliases: string[];
  description: string;
  semantic_type: 'categorical' | 'time' | 'identifier';
  expression?: string | null;
  expression_field_ids?: string[];
  /** 编译期自动生成的逻辑时间轴:各指标按各自声明的时间轴统计 */
  metric_time_axis?: boolean;
}

export interface AnalyticsMetricExpressionSource {
  name: string;
  field_id: string;
  expression?: string | null;
  expression_field_ids: string[];
  aggregation: AnalyticsMetric['aggregation'];
  filters: Array<{ field_id: string; operator: string; value: unknown }>;
}

export interface AnalyticsHierarchy {
  id: string;
  model_id: string;
  name: string;
  aliases: string[];
  description: string;
  levels: string[];
}

export interface AnalyticsMetric {
  id: string;
  name: string;
  model_id: string;
  kind: 'atomic' | 'derived';
  field_id: string | null;
  aggregation:
    | 'sum'
    | 'count'
    | 'count_distinct'
    | 'avg'
    | 'min'
    | 'max'
    | null;
  formula: string | null;
  /** 该指标沿哪条时间轴聚合(维度 id);空则回落数据集默认时间维度。 */
  agg_time_dimension_id?: string | null;
  aliases: string[];
  description: string;
  unit: string | null;
  format: string | null;
  requires_explicit_time: boolean;
  expression_sources?: AnalyticsMetricExpressionSource[];
}

export interface AnalyticsDataset {
  id: string;
  name: string;
  biz_name?: string | null;
  model_ids: string[];
  metric_ids: string[];
  dimension_ids: string[];
  default_limit: number;
  max_limit: number;
  aliases: string[];
  description: string;
  default_time_dimension_id: string | null;
  default_time_days: number | null;
  detail_time_default?: {
    unit: number;
    period: 'DAY' | 'WEEK' | 'MONTH' | 'QUARTER' | 'YEAR';
    time_mode: 'LAST' | 'RECENT' | 'CURRENT';
  } | null;
  aggregate_time_default?: {
    unit: number;
    period: 'DAY' | 'WEEK' | 'MONTH' | 'QUARTER' | 'YEAR';
    time_mode: 'LAST' | 'RECENT' | 'CURRENT';
  } | null;
  timezone: string;
}

export interface AnalyticsTopicPath {
  target_model_id: string;
  relation_ids: string[];
  prefix: string | null;
}

export interface AnalyticsTopicRoute {
  dataset_id: string;
  root_model_id: string;
  default_count_metric_id?: string | null;
  paths: AnalyticsTopicPath[];
  ai_context: string;
}

/**
 * Reviewed, immutable context carried by a Candidate/Release.
 *
 * This is deliberately structured instead of one free-form prompt: target,
 * kind and provenance remain visible during review and the query pipeline can
 * select only entries bound to the chosen scope.
 */
export interface AnalyticsSemanticContextEntry {
  id: string;
  target_type: 'project' | 'model' | 'metric' | 'dimension' | 'query_scope';
  target_id: string;
  kind: 'definition' | 'convention' | 'scope' | 'exception' | 'time_policy';
  text: string;
  source_type:
    | 'database_comment'
    | 'profile_evidence'
    | 'knowledge_document'
    | 'human_convention'
    | 'catalog_description';
  source_ref: string | null;
}

export interface AnalyticsQueryScopeCompilationDiagnostic {
  dataset_id: string;
  root_model_id: string;
  model_ids: string[];
  metric_ids: string[];
  dimension_ids: string[];
  default_count_metric_id: string | null;
  path_relation_ids: string[][];
  canonical_names: Record<string, string>;
  exclusions: Array<{
    element_id: string;
    reason_code: string;
  }>;
}

export interface AnalyticsTerm {
  id: string;
  name: string;
  description: string;
  aliases: string[];
  dataset_ids: string[];
  metric_ids: string[];
  dimension_ids: string[];
}

export interface AnalyticsCatalogDimension {
  id: string;
  name: string;
  bizName: string;
  description: string;
  status?: number | null;
  typeEnum?: string | null;
  sensitiveLevel: number;
  modelId: string;
  type: 'categorical' | 'time' | 'partition_time' | string;
  expr: string;
  semanticType: 'CATEGORY' | 'ID' | 'DATE' | string;
  alias: string | null;
  defaultValues: string[];
  dimValueMaps: Array<{
    techName: string;
    bizName: string;
    alias: string[];
    value?: string | null;
  }>;
  dataType?: string | null;
  ext: Record<string, unknown>;
  typeParams?: {
    isPrimary: string;
    timeGranularity: string;
  } | null;
}

export type AnalyticsCatalogMetricDefineType = 'FIELD' | 'MEASURE' | 'METRIC';

export interface AnalyticsCatalogMetric {
  id: string;
  name: string;
  bizName: string;
  description: string;
  status?: number | null;
  typeEnum?: string | null;
  sensitiveLevel: number;
  modelId: string;
  alias: string | null;
  dataFormatType?: string | null;
  dataFormat?: {
    needMultiply100: boolean;
    decimalPlaces?: number | null;
  } | null;
  classifications: string[];
  isTag: number;
  /** 该指标沿哪条时间轴聚合(维度 id)。留空时回落到数据集默认时间维度。 */
  aggTimeDimensionId?: string | null;
  ext: Record<string, unknown>;
  metricDefineType: AnalyticsCatalogMetricDefineType;
  metricDefineByFieldParams?: {
    expr: string;
    filterSql?: string | null;
    fields: Array<{ fieldName: string }>;
  } | null;
  metricDefineByMeasureParams?: {
    expr: string;
    filterSql?: string | null;
    measures: Array<{
      name: string;
      agg: string;
      expr: string;
      bizName: string;
      isCreateMetric: number;
      constraint?: string | null;
      alias?: string | null;
      unit?: string | null;
    }>;
  } | null;
  metricDefineByMetricParams?: {
    expr: string;
    filterSql?: string | null;
    metrics: Array<{ id: string; bizName: string }>;
  } | null;
}

export interface AnalyticsCatalogMeasure {
  name: string;
  agg: string;
  expr: string;
  bizName: string;
  isCreateMetric: number;
  constraint?: string | null;
  alias?: string | null;
  unit?: string | null;
}

export interface AnalyticsCatalogIdentifier {
  name: string;
  type: 'primary' | 'foreign';
  bizName: string;
  isCreateDimension: number;
}

export interface AnalyticsCatalogModelDimension {
  name: string;
  type:
    | 'categorical'
    | 'time'
    | 'partition_time'
    | 'primary_key'
    | 'foreign_key';
  expr: string;
  dateFormat: string;
  dataType?: string | null;
  typeParams?: {
    isPrimary: string;
    timeGranularity: string;
  } | null;
  isCreateDimension: number;
  bizName: string;
  description: string;
}

export interface AnalyticsCatalogSqlVariable {
  name: string;
  valueType: 'STRING' | 'NUMBER' | 'EXPR';
  defaultValues: unknown[];
}

export interface AnalyticsCatalogModel {
  id: string;
  name: string;
  bizName: string;
  description: string;
  status?: number | null;
  typeEnum?: string | null;
  sensitiveLevel: number;
  databaseId?: string | null;
  domainId?: string | null;
  filterSql?: string | null;
  isOpen?: number | null;
  alias?: string | null;
  sourceType?: string | null;
  modelDetail: {
    queryType: 'table_query' | 'sql_query';
    dbType?: string | null;
    sqlQuery?: string | null;
    tableQuery?: string | null;
    filterSql?: string | null;
    identifiers: AnalyticsCatalogIdentifier[];
    dimensions: AnalyticsCatalogModelDimension[];
    measures: AnalyticsCatalogMeasure[];
    fields: Array<{ fieldName: string; dataType: string }>;
    sqlVariables: AnalyticsCatalogSqlVariable[];
  };
  viewers: string[];
  viewOrgs: string[];
  admins: string[];
  adminOrgs: string[];
  ext: Record<string, unknown>;
}

export interface AnalyticsCatalogDataset {
  id: string;
  name: string;
  bizName: string;
  description: string;
  status?: number | null;
  typeEnum?: string | null;
  sensitiveLevel: number;
  domainId?: string | null;
  alias?: string | null;
  dataSetDetail: {
    dataSetModelConfigs: Array<{
      id: string;
      includesAll: boolean;
      metrics: string[];
      dimensions: string[];
    }>;
  };
  queryConfig: {
    detailTypeDefaultConfig: {
      timeDefaultConfig: {
        unit: number;
        period: 'DAY' | 'WEEK' | 'MONTH' | 'QUARTER' | 'YEAR';
        timeMode: 'LAST' | 'RECENT' | 'CURRENT';
      };
      limit: number;
    };
    aggregateTypeDefaultConfig: {
      timeDefaultConfig: {
        unit: number;
        period: 'DAY' | 'WEEK' | 'MONTH' | 'QUARTER' | 'YEAR';
        timeMode: 'LAST' | 'RECENT' | 'CURRENT';
      };
      limit: number;
    };
  };
  admins: string[];
  adminOrgs: string[];
}

export interface AnalyticsSemanticCatalog {
  projectId: string;
  revisionId: string;
  contractVersion: string;
  models: AnalyticsCatalogModel[];
  modelRelations: AnalyticsCatalogRelation[];
  dimensions: AnalyticsCatalogDimension[];
  hierarchies?: AnalyticsCatalogHierarchy[];
  metrics: AnalyticsCatalogMetric[];
  dataSets: AnalyticsCatalogDataset[];
  terms: AnalyticsTerm[];
  dimensionValues: AnalyticsDimensionValue[];
  semanticContext?: AnalyticsSemanticContextEntry[];
  analysisTopicRoutes?: AnalyticsTopicRoute[];
  queryRules?: AnalyticsQueryRule[];
}

/** 同一把尺子上由粗到细的一组维度,例如「行政区划」= 省 > 市 > 区。 */
export interface AnalyticsCatalogHierarchy {
  id: string;
  modelId: string;
  name: string;
  alias?: string | null;
  description?: string;
  /** 由粗到细的维度 id,至少两级。 */
  levels: string[];
}

export interface AnalyticsQueryRule {
  id: string;
  datasetId: string;
  priority: 0 | 1 | 2 | 3;
  ruleType: 'ADD_DATE' | 'ADD_SELECT';
  mode: 'BEFORE' | 'RECENT' | 'EXIST';
  parameters: Array<string | number>;
  outputs: string[];
  enabled: boolean;
}

export interface AnalyticsDimensionValue {
  id: string;
  dimension_id: string;
  value: string | number | boolean;
  display_name: string;
  aliases: string[];
  enabled: boolean;
}

export interface AnalyticsSemanticSpec {
  models: AnalyticsModel[];
  fields: AnalyticsField[];
  relations: AnalyticsRelation[];
  dimensions: AnalyticsDimension[];
  hierarchies?: AnalyticsHierarchy[];
  metrics: AnalyticsMetric[];
  datasets: AnalyticsDataset[];
  terms?: AnalyticsTerm[];
  dimension_values?: AnalyticsDimensionValue[];
  semantic_context?: AnalyticsSemanticContextEntry[];
  analysis_topic_routes?: AnalyticsTopicRoute[];
  query_rules?: Array<{
    id: string;
    dataset_id: string;
    priority: 0 | 1 | 2 | 3;
    rule_type: 'ADD_DATE' | 'ADD_SELECT';
    mode: 'BEFORE' | 'RECENT' | 'EXIST';
    parameters: Array<string | number>;
    outputs: string[];
    enabled: boolean;
  }>;
}

export interface AnalyticsSuggestion {
  id: string;
  target_kind: 'model' | 'field' | 'relation';
  target_id: string;
  changes: Record<string, unknown>;
  source: 'rule' | 'ai_schema' | 'ai_knowledge' | 'database_constraint';
  confidence: number;
  reason: string;
  evidence?: Array<{
    knowledgebase_id: string;
    document_id: string;
    document_revision: string;
    chunk_id: string;
    quote_hash: string;
    citation: string;
  }>;
  high_impact: boolean;
  state: 'pending' | 'accepted' | 'rejected' | 'conflict';
}

export interface AnalyticsSuggestionRun {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  status: 'completed';
  input_hash: string;
  suggestions: AnalyticsSuggestion[];
}

export interface AnalyticsSuggestionDecision {
  suggestion_id: string;
  accept: boolean;
  overrides: Record<string, unknown>;
}

export interface AnalyticsSemanticAliasReview {
  resource_type: 'dimension' | 'metric' | 'dimension_value';
  resource_id: string;
  aliases: string[];
  display_name?: string | null;
}

export interface AnalyticsModelingProposal {
  id: string;
  project_id: string;
  revision_id: string;
  suggestion_run_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  semantic_spec_hash: string;
  etag: number;
  status: 'draft' | 'applied';
  suggestions: AnalyticsSuggestion[];
  decisions: AnalyticsSuggestionDecision[];
  artifact: {
    base_semantic_spec_hash: string;
    dimension_values: AnalyticsDimensionValue[];
    alias_drafts: Array<{
      resource_type: 'dimension' | 'metric' | 'dimension_value';
      resource_id: string;
      resource_name: string;
      aliases: string[];
      display_name?: string | null;
    }>;
    default_count_metrics: Array<{
      id: string;
      name: string;
    }>;
    /** Wire-compatible storage for compiler-owned QueryScope projections. */
    analysis_topic_datasets: AnalyticsDataset[];
    analysis_topic_routes: AnalyticsTopicRoute[];
    /** Reviewed context bound into the backend artifact hash. */
    semantic_context: AnalyticsSemanticContextEntry[];
    query_scope_compiler_version: string;
    query_scope_compilation_hash: string | null;
    query_scope_diagnostics: AnalyticsQueryScopeCompilationDiagnostic[];
    artifact_hash: string;
  };
  reviewed_artifact_hash?: string | null;
  proposal_hash: string;
  resulting_revision_etag?: number | null;
}

export type AnalyticsQualityStatus =
  | 'passed'
  | 'warning'
  | 'blocking'
  | 'pending_review'
  | 'confirmed'
  | 'rejected';

export interface AnalyticsModelingQualityReport {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  semantic_spec_hash: string;
  etag: number;
  status: 'completed' | 'reviewed';
  content_hash: string;
  ready: boolean;
  blocking_count: number;
  warning_count: number;
  model_grains: Array<{
    model_id: string;
    identifier_field_ids: string[];
    total_rows: number;
    null_rows: number;
    distinct_non_null_keys: number;
    duplicate_rows: number;
    uniqueness_rate: number;
    null_rate: number;
    status: AnalyticsQualityStatus;
    message: string;
  }>;
  relations: Array<{
    relation_id: string;
    left_rows: number;
    right_rows: number;
    matched_left_rows: number;
    matched_right_rows: number;
    orphan_left_rows: number;
    orphan_right_rows: number;
    left_join_coverage: number;
    right_join_coverage: number;
    max_left_key_multiplicity: number;
    max_right_key_multiplicity: number;
    joined_rows: number;
    left_fanout_factor: number;
    right_fanout_factor: number;
    declared_cardinality: string;
    observed_cardinality?: string | null;
    status: AnalyticsQualityStatus;
    message: string;
  }>;
  metric_previews: Array<{
    id: string;
    dataset_id: string;
    metric_id: string;
    columns: string[];
    rows: unknown[][];
    status: AnalyticsQualityStatus;
    error_code?: string | null;
    message: string;
    review_note: string;
  }>;
  reachability: Array<{
    dataset_id: string;
    metric_id: string;
    dimension_id: string;
    metric_model_id: string;
    dimension_model_id: string;
    relation_ids: string[];
    status: AnalyticsQualityStatus;
    reason_code: string;
    message: string;
  }>;
}

export interface AnalyticsSchemaDriftReport {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  baseline_schema_hash: string;
  current_schema_hash: string;
  semantic_spec_hash: string;
  content_hash: string;
  ready: boolean;
  blocking_count: number;
  warning_count: number;
  changes: Array<{
    change_type: string;
    schema_name: string;
    table_name: string;
    column_name?: string | null;
    before?: unknown;
    after?: unknown;
    severity: 'info' | 'warning' | 'blocking';
    message: string;
    impacts: Array<{
      resource_kind: string;
      resource_id: string;
      reason: string;
    }>;
  }>;
}

export interface AnalyticsDimensionValueCandidate {
  id: string;
  dimension_value_id: string;
  dimension_id: string;
  value: string | number | boolean;
  frequency: number | null;
  observed: boolean;
  current: boolean;
  display_name: string;
  aliases: string[];
  enabled: boolean;
  list_state: 'normal' | 'black' | 'white';
}

export interface AnalyticsDimensionDictionaryPolicy {
  dimension_id: string;
  visible: boolean;
  ai_aliases: boolean;
  refresh_interval: 'manual' | 'daily' | 'weekly';
  black_list: Array<string | number | boolean>;
  white_list: Array<string | number | boolean>;
  refreshed_at: string | null;
  next_refresh_at: string | null;
}

export interface AnalyticsDimensionDictionaryEligibility {
  dimension_id: string;
  status: 'eligible' | 'review' | 'ineligible';
  reason_code: string;
  message: string;
  observed_distinct_values: number | null;
}

export interface AnalyticsDimensionDictionaryPreview {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  semantic_spec_hash: string;
  selected_dimension_ids: string[];
  policies: AnalyticsDimensionDictionaryPolicy[];
  eligibilities: AnalyticsDimensionDictionaryEligibility[];
  status: 'completed' | 'applied';
  candidates: AnalyticsDimensionValueCandidate[];
}

export interface AnalyticsDimensionValueDecision {
  candidate_id: string;
  accept: boolean;
  display_name?: string;
  aliases?: string[];
  enabled?: boolean;
  list_state?: 'normal' | 'black' | 'white';
}

export interface AnalyticsRevision {
  id: string;
  project_id: string;
  schema_snapshot_hash: string;
  etag: number;
  state: 'draft' | 'validated' | 'frozen' | 'published';
  semantic_spec: AnalyticsSemanticSpec;
  semantic_catalog: AnalyticsSemanticCatalog;
  suggestions: AnalyticsSuggestion[];
}

export type AnalyticsCatalogResourceKind =
  | 'models'
  | 'relations'
  | 'dimensions'
  | 'metrics'
  | 'datasets'
  | 'terms'
  | 'hierarchies'
  | 'query-rules';

export interface AnalyticsCatalogDeletionEffect {
  action: 'delete' | 'unlink';
  resource_kind:
    | 'model'
    | 'relation'
    | 'dimension'
    | 'metric'
    | 'dataset'
    | 'term'
    | 'dimension_value'
    | 'semantic_context'
    | 'hierarchy'
    | 'query_rule';
  resource_id: string;
  reason: string;
}

export interface AnalyticsCatalogDeletionImpact {
  resource_kind: Exclude<
    AnalyticsCatalogDeletionEffect['resource_kind'],
    'dimension_value'
  >;
  resource_id: string;
  source_catalog_hash: string;
  impact_hash: string;
  requires_confirmation: boolean;
  effects: AnalyticsCatalogDeletionEffect[];
}

export interface AnalyticsModelingDiagnostic {
  diagnostic_code: string;
  title: string;
  message: string;
  resource_kind:
    | 'revision'
    | 'model'
    | 'relation'
    | 'dimension'
    | 'metric'
    | 'dataset'
    | 'golden_suite';
  affected_resource_ids: string[];
  decision_kind: string;
  blocking: boolean;
  recommended_action: string;
}

export interface AnalyticsModelingDiagnostics {
  project_id: string;
  revision_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  semantic_spec_hash: string;
  ready: boolean;
  blocking_count: number;
  warning_count: number;
  diagnostics: AnalyticsModelingDiagnostic[];
}

export interface AnalyticsDraftResult {
  snapshot: {
    id: string;
    content_hash: string;
    table_count: number;
  };
  revision: AnalyticsRevision;
  warnings: Array<{ code: string; message: string }>;
}

export interface AnalyticsQueryFilter {
  dimension_id: string;
  operator:
    | 'eq'
    | 'ne'
    | 'gt'
    | 'gte'
    | 'lt'
    | 'lte'
    | 'in'
    | 'not_in'
    | 'between'
    | 'like'
    | 'is_null'
    | 'is_not_null';
  value: unknown;
}

export interface AnalyticsQueryOrder {
  element_id: string;
  direction: 'asc' | 'desc';
}

export interface AnalyticsAggregationOverride {
  metric_id: string;
  aggregation: 'sum' | 'count' | 'count_distinct' | 'avg' | 'min' | 'max';
}

export interface AnalyticsMetricQueryFilter {
  metric_id: string;
  operator: 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte';
  value: number;
}

export interface AnalyticsSemanticQuery {
  dataset_id: string;
  query_type: 'detail' | 'aggregate';
  metric_ids: string[];
  aggregation_overrides: AnalyticsAggregationOverride[];
  dimension_ids: string[];
  filters: AnalyticsQueryFilter[];
  measure_filters: AnalyticsMetricQueryFilter[];
  metric_filters: AnalyticsMetricQueryFilter[];
  order_by: AnalyticsQueryOrder[];
  limit: number | null;
}

interface AnalyticsQueryResponseBase {
  query_id: string;
  release_id: string;
  spec_hash: string;
  index_snapshot_id: string | null;
  trace: Array<{
    stage: string;
    status: 'started' | 'completed' | 'failed' | 'clarification';
    detail: Record<string, unknown>;
  }>;
  diagnostics?: {
    category:
      | 'success'
      | 'model_version'
      | 'mapping'
      | 'ambiguity'
      | 'final_parsing'
      | 'rule_fallback'
      | 'correction'
      | 'translation'
      | 'routing'
      | 'sql_guard'
      | 'database_execution'
      | 'internal';
    stage: string;
    severity: 'info' | 'warning' | 'error';
    summary: string;
    recommendation: string;
    /** 给提问者看的下一步；recommendation 是给建模者看的。 */
    user_hint?: string;
  } | null;
}

export interface AnalyticsCompletedQueryResponse
  extends AnalyticsQueryResponseBase {
  state: 'COMPLETED';
  interpretation: {
    dataset_id: string;
    query_type: 'detail' | 'aggregate';
    metrics: string[];
    dimensions: string[];
    filters: string[];
    applied_defaults: string[];
  };
  semantic_query: AnalyticsSemanticQuery;
  /** 由 LLM 裁决的异名歧义；旧版服务不返回。 */
  resolved_by_llm?: AnalyticsResolvedAmbiguity[];
  /** V2 统一披露 AI、记忆、人工或最终 LLM 的业务理解。 */
  semantic_decisions?: AnalyticsSemanticDecision[];
  parsed_s2sql: string;
  corrected_s2sql: string;
  physical_sql?: string | null;
  data: {
    columns: string[];
    rows: unknown[][];
    row_count: number;
    truncated: boolean;
  };
}

export interface AnalyticsClarificationOption {
  /** Opaque continuation token; clients must return it unchanged and never parse it. */
  candidate_id: string;
  kind: 'metric' | 'dimension' | 'dimension_value' | 'analysis_object';
  label: string;
  description: string;
}

/** 一处异名歧义（「人数」→ 生还人数 / 遇难人数）由最终 LLM 裁决的结果。 */
export interface AnalyticsResolvedAmbiguity {
  detected_text: string;
  chosen: AnalyticsClarificationOption;
  alternatives: AnalyticsClarificationOption[];
}

export interface AnalyticsSemanticDecision {
  source: 'human' | 'ai' | 'memory' | 'final_llm';
  detected_text: string;
  chosen: AnalyticsClarificationOption;
  alternatives: AnalyticsClarificationOption[];
}

export interface AnalyticsClarificationQueryResponse
  extends AnalyticsQueryResponseBase {
  state: 'CLARIFICATION_REQUIRED';
  question: string;
  options: AnalyticsClarificationOption[];
}

export interface AnalyticsFailedQueryResponse
  extends AnalyticsQueryResponseBase {
  state: 'FAILED';
  error: {
    stage: string;
    code: string;
    message: string;
    retryable: boolean;
  };
}

export type AnalyticsQueryResponse =
  | AnalyticsCompletedQueryResponse
  | AnalyticsClarificationQueryResponse
  | AnalyticsFailedQueryResponse;

export type AnalyticsQueryDiagnosticStatus =
  | 'completed'
  | 'failed'
  | 'clarification'
  | 'started'
  | 'not_run'
  | 'not_recorded';

export interface AnalyticsQueryDiagnosticTimelineEvent {
  status: string;
  detail: Record<string, unknown>;
  /** Forward-compatible fields authored by newer core versions. */
  [key: string]: unknown;
}

export interface AnalyticsQueryDiagnosticTimelineItem {
  key: string;
  label: string;
  group: 'context' | 'query';
  status: AnalyticsQueryDiagnosticStatus;
  summary: string;
  events: AnalyticsQueryDiagnosticTimelineEvent[];
  artifacts: Record<string, unknown>;
  /** The UI preserves unknown server evidence instead of narrowing the API. */
  [key: string]: unknown;
}

export interface AnalyticsQueryDiagnosticSummary {
  query_id: string;
  state: string;
  mode: string;
  question: string;
  diagnostic_stage?: string | null;
  category?: string | null;
  /** Compatibility alias emitted by early v1 diagnostic exporters. */
  diagnostic_category?: string | null;
  message?: string | null;
  version_status?: string | null;
  [key: string]: unknown;
}

export interface AnalyticsQueryDiagnosticExport {
  filename: string;
  media_type: string;
  markdown: string;
  sha256: string;
  summary: AnalyticsQueryDiagnosticSummary;
  timeline: AnalyticsQueryDiagnosticTimelineItem[];
  [key: string]: unknown;
}

export interface AnalyticsGoldenCase {
  id: string;
  question: string;
  dataset_ids: string[];
  tags: string[];
  memory_status: 'PENDING' | 'ENABLED' | 'DISABLED';
  memory_review_result?: 'POSITIVE' | 'NEGATIVE';
  memory_review_comment: string;
  expected_state: 'COMPLETED' | 'FAILED';
  expected_dataset_id?: string;
  expected_query_type: 'detail' | 'aggregate';
  expected_metric_ids: string[];
  expected_aggregation_overrides: AnalyticsAggregationOverride[];
  expected_dimension_ids: string[];
  expected_filters: AnalyticsQueryFilter[];
  expected_measure_filters: AnalyticsMetricQueryFilter[];
  expected_metric_filters: AnalyticsMetricQueryFilter[];
  /** 缺席(None)表示不比较排序与 limit;空数组是「要求无排序」的显式期望。 */
  expected_order_by?: AnalyticsQueryOrder[];
  expected_limit?: number;
  expected_s2sql?: string;
  expected_rows?: unknown[][];
  row_order_matters: boolean;
  numeric_tolerance: string;
  expected_error_code?: string;
}

export interface AnalyticsGoldenSuiteRecord {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  schema_snapshot_hash: string;
  semantic_spec_hash: string;
  suite: {
    id: string;
    name: string;
    project_id: string;
    fixed_now: string | null;
    cases: AnalyticsGoldenCase[];
  };
  saved_by: string;
  updated_at: string;
}

export interface AnalyticsEvaluationReport {
  id: string;
  suite_id: string;
  project_id: string;
  release_id: string;
  spec_hash: string;
  index_snapshot_id: string;
  total: number;
  passed: number;
  accuracy: number;
  required_accuracy: number;
  gate_passed: boolean;
  silent_wrong_count?: number;
  false_accept_count?: number;
  false_refusal_count?: number;
  results: Array<{
    case_id: string;
    passed: boolean;
    failure_stage: 'state' | 'mapping' | 'semantic' | 'result' | 'error' | null;
    message: string;
  }>;
}

export interface AnalyticsPublishedRelease {
  release: {
    id: string;
    project_id: string;
    revision_id: string | null;
    spec_hash: string;
    index_snapshot_id: string | null;
    models: AnalyticsModel[];
    fields: AnalyticsField[];
    relations: AnalyticsRelation[];
    dimensions: AnalyticsDimension[];
    metrics: AnalyticsMetric[];
    datasets: AnalyticsDataset[];
    semantic_context?: AnalyticsSemanticContextEntry[];
    analysis_topic_routes: AnalyticsTopicRoute[];
  };
  status: 'active' | 'retired';
}

export interface AnalyticsModelingSummary {
  project_id: string;
  project_name: string;
  stage:
    | 'selecting_data'
    | 'building_draft'
    | 'reviewing_decisions'
    | 'blocked'
    | 'verifying'
    | 'ready_to_publish'
    | 'published';
  active_release_id: string | null;
  revision_id: string | null;
  revision_etag: number | null;
  revision_state: string | null;
  schema_snapshot_hash: string | null;
}

export type AnalyticsModelingJobStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface AnalyticsModelingJobTable {
  model_id: string;
  name: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  attempts: number;
  error: string | null;
}

export interface AnalyticsModelingJob {
  id: string;
  project_id: string;
  revision_id: string;
  revision_etag: number;
  status: AnalyticsModelingJobStatus;
  stage: 'queued' | 'modeling' | 'enriching' | 'done';
  progress: { tables: AnalyticsModelingJobTable[] };
  proposal_id: string | null;
  error: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}


export interface AnalyticsReleaseSummary {
  id: string;
  revision_id: string;
  spec_hash: string;
  status: 'active' | 'retired';
  created_at: string;
  /** 第几次发布，从 1 开始。界面显示这个，不显示 `rel_...`。 */
  sequence: number;
}

// --- 维度值字典 ------------------------------------------------------------

export interface AnalyticsDictionaryEligibility {
  dimension_id: string;
  status: 'eligible' | 'review' | 'ineligible' | string;
  reason_code: string;
  message: string;
  observed_distinct_values: number | null;
}

export interface AnalyticsDictionaryCandidate {
  id: string;
  dimension_value_id: string;
  dimension_id: string;
  value: string;
  frequency: number;
  observed: boolean;
  /** 目录里已存在(编辑既有值而不是新增)。 */
  current: boolean;
  display_name: string;
  aliases: string[];
  enabled: boolean;
  list_state: string;
}

export interface AnalyticsDictionaryPreview {
  id: string;
  revision_id: string;
  revision_etag: number;
  status: string;
  eligibilities: AnalyticsDictionaryEligibility[];
  candidates: AnalyticsDictionaryCandidate[];
}

export interface AnalyticsDictionaryDecision {
  candidate_id: string;
  accept: boolean;
  display_name?: string | null;
  aliases?: string[] | null;
  enabled?: boolean | null;
}

export type AnalyticsFeedbackStatus = 'open' | 'resolved' | 'ignored';

/**
 * 问数反馈列表的一行：**同一个说法**的所有记录并成一条。
 *
 * 这是后端聚合后的结果，不是原始记录行——聚合必须发生在分页之前。此前后端返回
 * 最新 50 行、由前端 group by，于是一个被问过 21 次的说法散在三页，第一页显示
 * 2 次、第二页 10 次、再往后 9 次；按次数排序排的其实是"这一页里出现了几次"，
 * 页头也报不出真实种数。
 */
export interface AnalyticsQueryFailure {
  kind?: 'refused' | 'clarified' | 'inferred' | 'unknown_value';
  /** 用户那个说法。空串表示这条没能提出说法，此时按问句聚合。 */
  phrase: string;
  /** 这次的正解：用户选中的成员名，或近似取值建议。没有就是空串。 */
  resolution: string;
  /** 代表问句。同一个说法可能出现在多句问话里，取最早的一句。 */
  question: string;
  code: string;
  message: string;
  /** 真实总次数，跨页。 */
  count: number;
  last_seen: string;
}
