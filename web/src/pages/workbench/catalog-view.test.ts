import { describe, expect, it } from 'vitest';
import type { AnalyticsSemanticCatalog, AnalyticsSemanticSpec } from '@analytics/api/types';
import {
  allSemanticContextEntries,
  buildCatalogInventory,
  buildCatalogModelRows,
  buildProposalScopeRows,
  buildScopeDiagnostics,
  normalizeWorkbenchStep,
  serializeCatalogResource,
  sliceCatalogRows,
  visibleSemanticContext,
} from './catalog-view';

const spec = {
  models: [
    {
      id: 'model-orders',
      name: '订单',
      table: 'orders',
      schema_name: 'public',
    },
    {
      id: 'model-merchants',
      name: '商家',
      table: 'merchants',
      schema_name: 'public',
    },
  ],
  fields: [
    {
      id: 'field-order-amount',
      model_id: 'model-orders',
      name: '订单金额字段',
      column: 'amount',
      data_type: 'numeric',
      kind: 'measure',
      identifier_type: null,
    },
  ],
  relations: [
    {
      id: 'relation-orders-merchants',
      left_model_id: 'model-orders',
      right_model_id: 'model-merchants',
    },
  ],
  dimensions: [
    { id: 'dimension-order-date', name: '下单日期', model_id: 'model-orders' },
    { id: 'dimension-merchant-name', name: '商家名称', model_id: 'model-merchants' },
  ],
  metrics: [
    { id: 'metric-gmv', name: '交易额', model_id: 'model-orders' },
    { id: 'metric-merchant-count', name: '商家数', model_id: 'model-merchants' },
  ],
  datasets: [
    {
      id: 'scope-orders',
      name: '订单问数范围',
      model_ids: ['model-orders', 'model-merchants'],
      metric_ids: ['metric-gmv'],
      dimension_ids: ['dimension-order-date', 'dimension-merchant-name'],
    },
    {
      id: 'scope-merchants',
      name: '商家问数范围',
      model_ids: ['model-merchants'],
      metric_ids: ['metric-merchant-count'],
      dimension_ids: ['dimension-merchant-name'],
    },
  ],
  analysis_topic_routes: [
    {
      dataset_id: 'scope-orders',
      root_model_id: 'model-orders',
      default_count_metric_id: null,
      ai_context: '',
      paths: [
        {
          target_model_id: 'model-merchants',
          relation_ids: ['relation-orders-merchants'],
          prefix: '商家',
        },
      ],
    },
    {
      dataset_id: 'scope-merchants',
      root_model_id: 'model-merchants',
      default_count_metric_id: 'metric-merchant-count',
      ai_context: '',
      paths: [],
    },
  ],
  semantic_context: [
    {
      id: 'context-project-currency',
      target_type: 'project',
      target_id: 'project-sales',
      kind: 'convention',
      text: '金额统一使用人民币。',
      source_type: 'human_convention',
      source_ref: null,
    },
    {
      id: 'context-scope-orders',
      target_type: 'query_scope',
      target_id: 'scope-orders',
      kind: 'scope',
      text: '订单范围仅覆盖已支付订单。',
      source_type: 'knowledge_document',
      source_ref: `sha256:${'a'.repeat(64)}`,
    },
  ],
  terms: [
    {
      id: 'term-gmv',
      name: 'GMV',
      description: '成交总额',
      aliases: ['成交额'],
      dataset_ids: ['scope-orders'],
      metric_ids: ['metric-gmv'],
      dimension_ids: [],
    },
  ],
} as unknown as AnalyticsSemanticSpec;

const catalog = {
  models: [
    {
      id: 'model-orders',
      name: '订单',
      bizName: 'orders',
      description: '订单事实',
      filterSql: "status = 'paid'",
      alias: '支付订单',
      ext: { owner: 'finance', retention_days: 365 },
      modelDetail: {
        queryType: 'table_query',
        fields: [{ fieldName: 'amount', dataType: 'numeric' }],
        dimensions: [],
        identifiers: [],
        measures: [
          {
            name: 'order_amount',
            bizName: '订单金额',
            agg: 'SUM',
            expr: 'amount',
            isCreateMetric: 1,
            constraint: "status = 'paid'",
            alias: '实付金额',
            unit: '元',
          },
        ],
      },
    },
    {
      id: 'model-merchants',
      name: '商家',
      bizName: 'merchants',
      description: '商家实体',
      modelDetail: {
        queryType: 'table_query',
        fields: [],
        dimensions: [],
        identifiers: [],
        measures: [],
      },
    },
  ],
  dimensions: [
    {
      id: 'dimension-order-date',
      name: 'order_date',
      bizName: '下单日期',
      modelId: 'model-orders',
      description: '支付日期',
      sensitiveLevel: 0,
      type: 'time',
      expr: 'paid_at',
      semanticType: 'DATE',
      alias: '付款日期',
      defaultValues: ['2026-08-01'],
      dimValueMaps: [],
      ext: { calendar: 'gregorian' },
      typeParams: { isPrimary: 'false', timeGranularity: 'day' },
    },
  ],
  metrics: [
    {
      id: 'metric-gmv',
      name: 'gmv',
      bizName: '交易额',
      modelId: 'model-orders',
      description: '支付交易额',
      sensitiveLevel: 0,
      alias: '成交额',
      dataFormatType: 'decimal',
      dataFormat: { needMultiply100: false, decimalPlaces: 2 },
      classifications: ['finance'],
      isTag: 0,
      ext: { audited: true },
      metricDefineType: 'MEASURE',
    },
  ],
  terms: [spec.terms?.[0]],
  dimensionValues: [
    {
      id: 'value-merchant-a',
      dimension_id: 'dimension-merchant-name',
      value: 'm-a',
      display_name: '甲商家',
      aliases: ['商家甲'],
      enabled: true,
    },
  ],
} as unknown as AnalyticsSemanticCatalog;

describe('complete semantic catalog projection', () => {
  it('folds legacy editable-topic routes into the complete catalog workflow', () => {
    expect(['canvas', 'ai', 'topics'].map(normalizeWorkbenchStep)).toEqual([
      'catalog',
      'catalog',
      'catalog',
    ]);
    expect(normalizeWorkbenchStep('publish')).toBe('publish');
    expect(normalizeWorkbenchStep('unknown')).toBe('tables');
  });

  it('exposes every governed metric and dimension under its model', () => {
    expect(buildCatalogModelRows(spec)).toEqual([
      expect.objectContaining({
        id: 'model-orders',
        metricNames: ['交易额'],
        dimensionNames: ['下单日期'],
      }),
      expect.objectContaining({
        id: 'model-merchants',
        metricNames: ['商家数'],
        dimensionNames: ['商家名称'],
      }),
    ]);
  });

  it('exposes all seven catalog resource kinds with governed DTO details', () => {
    const inventory = buildCatalogInventory(spec, catalog);
    expect({
      models: inventory.models.length,
      fields: inventory.fields.length,
      dimensions: inventory.dimensions.length,
      measures: inventory.measures.length,
      metrics: inventory.metrics.length,
      terms: inventory.terms.length,
      dimensionValues: inventory.dimensionValues.length,
    }).toEqual({
      models: 2,
      fields: 1,
      dimensions: 2,
      measures: 1,
      metrics: 2,
      terms: 1,
      dimensionValues: 1,
    });
    expect(inventory.measures[0]).toEqual(expect.objectContaining({
      title: '订单金额',
      subtitle: '订单',
      details: expect.arrayContaining([
        { label: '聚合', value: 'SUM' },
        { label: '表达式', value: 'amount' },
        { label: '单位', value: '元' },
      ]),
    }));
    expect(inventory.dimensionValues[0]).toEqual(expect.objectContaining({
      title: '甲商家',
      subtitle: '商家名称',
      aliases: ['商家甲'],
    }));
    expect(inventory.models[0].raw).toBe(catalog.models[0]);
    expect(inventory.fields[0].raw).toBe(spec.fields[0]);
    expect(inventory.dimensions[0].raw).toBe(catalog.dimensions[0]);
    expect(inventory.measures[0].raw).toBe(catalog.models[0].modelDetail.measures[0]);
    expect(inventory.metrics[0].raw).toBe(catalog.metrics[0]);
    expect(inventory.terms[0].raw).toBe(catalog.terms[0]);
    expect(inventory.dimensionValues[0].raw).toBe(catalog.dimensionValues[0]);

    expect(JSON.parse(serializeCatalogResource(inventory.models[0]))).toMatchObject({
      filterSql: "status = 'paid'",
      alias: '支付订单',
      ext: { owner: 'finance', retention_days: 365 },
    });
    expect(JSON.parse(serializeCatalogResource(inventory.measures[0]))).toMatchObject({
      constraint: "status = 'paid'",
      alias: '实付金额',
      unit: '元',
    });
  });

  it('handles an empty catalog and pages large resource lists without truncating counts', () => {
    const empty = buildCatalogInventory(
      {
        models: [], fields: [], relations: [], dimensions: [], metrics: [], datasets: [],
      } as AnalyticsSemanticSpec,
      {
        projectId: 'p', revisionId: 'r', contractVersion: 'v', models: [], modelRelations: [],
        dimensions: [], metrics: [], dataSets: [], terms: [], dimensionValues: [],
      },
    );
    expect(Object.values(empty).every((items) => items.length === 0)).toBe(true);

    const rows = Array.from({ length: 250 }, (_, index) => ({ id: String(index) }));
    expect(sliceCatalogRows(rows, 100)).toEqual({
      visible: rows.slice(0, 100),
      remaining: 150,
      nextLimit: 200,
    });
    expect(sliceCatalogRows(rows, 300)).toEqual({
      visible: rows,
      remaining: 0,
      nextLimit: 250,
    });
  });

  it('enumerates catalog resources even when an older normalized projection omits them', () => {
    const legacyProjection = { ...spec, terms: [] } as AnalyticsSemanticSpec;
    const inventory = buildCatalogInventory(legacyProjection, catalog);
    expect(inventory.terms.map((row) => row.id)).toEqual(['term-gmv']);
    expect(inventory.terms[0].raw).toBe(catalog.terms[0]);
  });

  it('keeps compatibility dataset ids while rendering QueryScope as read-only diagnostics', () => {
    expect(buildScopeDiagnostics(spec)).toEqual([
      expect.objectContaining({
        id: 'scope-orders',
        rootName: '订单',
        modelNames: ['订单', '商家'],
        metricNames: ['交易额'],
        dimensionNames: ['下单日期', '商家名称'],
        defaultCountName: null,
        pathLabels: ['订单 → 商家（商家；relation-orders-merchants）'],
      }),
      expect.objectContaining({
        id: 'scope-merchants',
        rootName: '商家',
        defaultCountName: '商家数',
        pathLabels: [],
      }),
    ]);
  });

  it('renders backend-authored QueryScope compilation evidence without dropping names or exclusions', () => {
    expect(buildProposalScopeRows(spec, spec.datasets, [
      {
        dataset_id: 'scope-orders',
        root_model_id: 'model-orders',
        model_ids: ['model-orders', 'model-merchants'],
        metric_ids: ['metric-gmv'],
        dimension_ids: ['dimension-order-date', 'dimension-merchant-name'],
        default_count_metric_id: null,
        path_relation_ids: [['relation-orders-merchants']],
        canonical_names: {
          'metric-gmv': '交易额',
          'dimension-merchant-name': '商家.商家名称',
        },
        exclusions: [{ element_id: 'metric-merchant-count', reason_code: 'ROOT_ONLY_METRIC' }],
      },
    ])).toEqual([
      expect.objectContaining({
        datasetName: '订单问数范围',
        rootName: '订单',
        modelNames: ['订单', '商家'],
        metricNames: ['交易额'],
        dimensionNames: ['下单日期', '商家名称'],
        defaultCountName: null,
        pathLabels: ['订单 → 商家'],
        canonicalNames: [
          ['dimension-merchant-name', '商家.商家名称'],
          ['metric-gmv', '交易额'],
        ],
        exclusions: [['商家数', 'ROOT_ONLY_METRIC']],
      }),
    ]);
  });

  it('never hides reviewed semantic context by target or source kind', () => {
    expect(visibleSemanticContext(spec).map((entry) => entry.id)).toEqual([
      'context-project-currency',
      'context-scope-orders',
    ]);
    expect(allSemanticContextEntries(spec.semantic_context).map((entry) => entry.id)).toEqual([
      'context-project-currency',
      'context-scope-orders',
    ]);
  });
});
