import {
  Children,
  createElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type {
  AnalyticsClarificationOption,
  AnalyticsClarificationQueryResponse,
  AnalyticsCompletedQueryResponse,
  AnalyticsFailedQueryResponse,
} from '@analytics/api/types';
import type { QueryInput } from '@analytics/api/analytics';
import {
  buildClarificationContinuation,
  QueryAnswer,
  type QueryTurn,
} from './query-answer';

const RESPONSE_BASE = {
  query_id: 'query-1',
  release_id: 'release-1',
  spec_hash: 'sha256:release',
  index_snapshot_id: 'snapshot-1',
  trace: [],
};

function clarificationResponse(
  options: AnalyticsClarificationOption[],
): AnalyticsClarificationQueryResponse {
  return {
    ...RESPONSE_BASE,
    state: 'CLARIFICATION_REQUIRED',
    question: '请选择你想分析的业务对象',
    options,
  };
}

function renderAnswer(
  response: QueryTurn['response'],
  onChoose = vi.fn(),
  target: QueryTurn['target'] = { mode: 'release' },
) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(QueryAnswer, {
        projectId: 'project-1',
        turn: { id: 'turn-1', question: '看一下经营情况', response, target },
        columnName: (id: string) => id,
        onChoose,
      }),
    ),
  );
}

function elementsIn(node: ReactNode): ReactElement[] {
  const found: ReactElement[] = [];
  Children.forEach(node, (child) => {
    if (!isValidElement(child)) return;
    found.push(child);
    elementsIn((child.props as { children?: ReactNode }).children).forEach((item) =>
      found.push(item),
    );
  });
  return found;
}

describe('QueryAnswer clarification cards', () => {
  const options: AnalyticsClarificationOption[] = [
    {
      candidate_id: 'opaque/%:not-a-schema-id?choice=metric',
      kind: 'metric',
      label: '成交金额',
      description: '已支付订单的含税成交金额，按退款后口径计算',
    },
    {
      candidate_id: 'opaque/%:not-a-schema-id?choice=dimension',
      kind: 'dimension',
      label: '客户所属区域',
      description: '客户当前归属的销售大区',
    },
    {
      candidate_id: 'opaque/%:not-a-schema-id?choice=value',
      kind: 'dimension_value',
      label: '华东区',
      description: '客户所属区域 = 华东区',
    },
    {
      candidate_id: 'opaque/%:not-a-schema-id?choice=object',
      kind: 'analysis_object',
      label: '门店经营',
      description: '围绕门店经营事实进行分析',
    },
  ];

  it('renders typed semantic choices as wrapping vertical business cards', () => {
    const html = renderAnswer(clarificationResponse(options));

    expect(html).toContain('请选择你想分析的业务对象');
    expect(html).toContain('指标');
    expect(html).toContain('维度');
    expect(html).toContain('维度值');
    expect(html).toContain('分析对象');
    expect(html).toContain('成交金额');
    expect(html).toContain('已支付订单的含税成交金额，按退款后口径计算');
    expect(html).toContain(
      'role="group" aria-label="可选业务语义" class="flex flex-col gap-2"',
    );
    expect(html.match(/<button[^>]*class="[^"]*w-full[^"]*"/g)).toHaveLength(4);
    expect(html).toContain('whitespace-normal');
    expect(html).not.toContain('internal-sales-scope');
    expect(html).not.toContain('internal-store-scope');
    expect(html).not.toContain('查询作用域');
    expect(html).not.toContain('opaque/%:not-a-schema-id');
  });

  it('passes the untouched opaque option and response to onChoose', () => {
    const response = clarificationResponse(options);
    const onChoose = vi.fn();
    const tree = QueryAnswer({
      projectId: 'project-1',
      turn: { id: 'turn-1', question: '看一下经营情况', response },
      columnName: (id) => id,
      onChoose,
    });
    const choice = elementsIn(tree).find(
      (element) =>
        typeof (element.props as { onClick?: unknown }).onClick === 'function' &&
        (element.props as { children?: ReactNode }).children !== undefined,
    );

    expect(choice).toBeDefined();
    (choice!.props as { onClick: () => void }).onClick();
    expect(onChoose).toHaveBeenCalledOnce();
    expect(onChoose).toHaveBeenCalledWith(options[0], response);
  });
});

describe('buildClarificationContinuation', () => {
  const initialInput: QueryInput = {
    question: '华东销售额是多少',
    dataset_ids: ['scope-orders', 'scope-order-items'],
    conversation_id: 'conversation-1',
  };
  const response = clarificationResponse([]);

  it('continues a semantic selection through a later composed business-object token', () => {
    const valueInput = buildClarificationContinuation(
      initialInput,
      {
        candidate_id: 'opaque-value-token',
        kind: 'dimension_value',
        label: '华东',
        description: '地区 = 华东',
      },
      response,
    );
    const routedInput = buildClarificationContinuation(
      valueInput,
      {
        // The service binds the previous semantic selection and the chosen
        // business object into this release-bound opaque continuation token.
        candidate_id: 'opaque-composed-business-object-token',
        kind: 'analysis_object',
        label: '订单',
        description: '围绕订单事实分析',
      },
      response,
    );

    expect(valueInput.selected_candidate_id).toBe('opaque-value-token');
    expect(valueInput.dataset_ids).toEqual(['scope-orders', 'scope-order-items']);
    expect(routedInput).toEqual({
      ...valueInput,
      selected_candidate_id: 'opaque-composed-business-object-token',
      expected_release_id: response.release_id,
      expected_spec_hash: response.spec_hash,
      expected_index_snapshot_id: response.index_snapshot_id ?? undefined,
    });
  });

  it('uses the current semantic card token without reopening a previously narrowed business object', () => {
    const narrowedInput = { ...initialInput, dataset_ids: ['scope-orders'] };
    const routedInput = buildClarificationContinuation(
      narrowedInput,
      {
        candidate_id: 'opaque-business-object-token',
        kind: 'analysis_object',
        label: '订单',
        description: '围绕订单事实分析',
      },
      response,
    );
    const metricInput = buildClarificationContinuation(
      routedInput,
      {
        candidate_id: 'opaque-metric-token',
        kind: 'metric',
        label: '订单净金额',
        description: '退款后订单金额',
      },
      response,
    );

    expect(routedInput.dataset_ids).toEqual(['scope-orders']);
    expect(routedInput.selected_candidate_id).toBe('opaque-business-object-token');
    expect(metricInput.dataset_ids).toEqual(['scope-orders']);
    expect(metricInput.selected_candidate_id).toBe('opaque-metric-token');
    expect(metricInput.conversation_id).toBe(initialInput.conversation_id);
  });

  it('updates the version binding without mutating the originating input', () => {
    const staleOrigin: QueryInput = {
      ...initialInput,
      selected_candidate_id: 'opaque-old-token',
      expected_release_id: 'release-old',
      expected_spec_hash: 'sha256:old',
      expected_index_snapshot_id: 'snapshot-old',
    };

    const continued = buildClarificationContinuation(
      staleOrigin,
      {
        candidate_id: 'opaque-current-token',
        kind: 'metric',
        label: '订单净金额',
        description: '退款后订单金额',
      },
      response,
    );

    expect(continued.expected_release_id).toBe('release-1');
    expect(continued.expected_spec_hash).toBe('sha256:release');
    expect(continued.expected_index_snapshot_id).toBe('snapshot-1');
    expect(staleOrigin.selected_candidate_id).toBe('opaque-old-token');
  });
});

describe('QueryAnswer completed result', () => {
  it('keeps the internal query scope out of the normal answer summary', () => {
    const response: AnalyticsCompletedQueryResponse = {
      ...RESPONSE_BASE,
      state: 'COMPLETED',
      interpretation: {
        dataset_id: 'dataset-internal-id',
        query_type: 'aggregate',
        metrics: ['成交金额'],
        dimensions: ['客户所属区域'],
        filters: ['客户所属区域 = 华东区'],
        applied_defaults: [],
      },
      semantic_query: {
        dataset_id: 'dataset-internal-id',
        query_type: 'aggregate',
        metric_ids: ['metric-revenue'],
        aggregation_overrides: [],
        dimension_ids: ['dimension-region'],
        filters: [],
        measure_filters: [],
        metric_filters: [],
        order_by: [],
        limit: null,
      },
      parsed_s2sql: 'SELECT 成交金额 BY 客户所属区域',
      corrected_s2sql: 'SELECT 成交金额 BY 客户所属区域',
      physical_sql: null,
      data: {
        columns: ['dimension-region', 'metric-revenue'],
        rows: [['华东区', 100]],
        row_count: 1,
        truncated: false,
      },
    };

    const html = renderAnswer(response);

    expect(html).toContain('成交金额');
    expect(html).toContain('按 客户所属区域');
    expect(html).not.toContain('查询作用域');
    expect(html).not.toContain('内部订单事实根');
    expect(html).not.toContain('dataset-internal-id');
    expect(html).toContain('一键诊断');
    expect(html.match(/一键诊断/g)).toHaveLength(1);
    expect(html).not.toContain('查看 S2SQL 与物理 SQL');

    const draftHtml = renderAnswer(
      response,
      vi.fn(),
      {
        mode: 'draft',
        revisionId: 'revision-1',
        version: { expected_etag: 1, schema_snapshot_hash: 'sha256:schema' },
      },
    );
    expect(draftHtml).toContain('查看 S2SQL 与物理 SQL');
    expect(draftHtml.indexOf('一键诊断')).toBeLessThan(
      draftHtml.indexOf('查看 S2SQL 与物理 SQL'),
    );
  });

  it('discloses automatic semantic decisions as switchable business chips', () => {
    const alternative: AnalyticsClarificationOption = {
      candidate_id: 'opaque-switch-token',
      kind: 'metric',
      label: '订单退款金额',
      description: '订单退款口径',
    };
    const response: AnalyticsCompletedQueryResponse = {
      ...RESPONSE_BASE,
      state: 'COMPLETED',
      interpretation: {
        dataset_id: 'dataset-internal-id',
        query_type: 'aggregate',
        metrics: ['订单净金额'],
        dimensions: ['地区'],
        filters: [],
        applied_defaults: [],
      },
      semantic_query: {
        dataset_id: 'dataset-internal-id',
        query_type: 'aggregate',
        metric_ids: ['metric-net'],
        aggregation_overrides: [],
        dimension_ids: ['dimension-region'],
        filters: [],
        measure_filters: [],
        metric_filters: [],
        order_by: [],
        limit: null,
      },
      semantic_decisions: [
        {
          source: 'ai',
          detected_text: '订单收入',
          chosen: {
            candidate_id: 'opaque-chosen-token',
            kind: 'metric',
            label: '订单净金额',
            description: '订单实际收入',
          },
          alternatives: [alternative],
        },
        {
          source: 'ai',
          detected_text: '业务记录粒度',
          chosen: {
            candidate_id: 'opaque-orders-token',
            kind: 'analysis_object',
            label: '订单',
            description: '每张订单一条',
          },
          alternatives: [],
        },
      ],
      parsed_s2sql: 'SELECT 地区, SUM(订单净金额) FROM orders分析 GROUP BY 地区',
      corrected_s2sql: 'SELECT 地区, SUM(订单净金额) FROM orders分析 GROUP BY 地区',
      physical_sql: null,
      data: {
        columns: ['dimension-region', 'metric-net'],
        rows: [['华东', 500]],
        row_count: 1,
        truncated: false,
      },
    };
    const onChoose = vi.fn();
    const tree = QueryAnswer({
      projectId: 'project-1',
      turn: { id: 'turn-1', question: '各地区订单收入', response },
      columnName: (id) => id,
      onChoose,
    });
    const html = renderAnswer(response, onChoose);

    expect(html).toContain('自动理解「订单收入」为');
    expect(html).toContain('订单净金额');
    expect(html).toContain('按订单分析');
    expect(html).toContain('切换为「订单退款金额」');
    expect(html).not.toContain('opaque-switch-token');
    expect(html).not.toContain('dataset-internal-id');

    const switchButton = elementsIn(tree).find(
      (element) =>
        typeof (element.props as { onClick?: unknown }).onClick === 'function' &&
        String((element.props as { children?: ReactNode }).children).includes('订单退款金额'),
    );
    expect(switchButton).toBeDefined();
    (switchButton!.props as { onClick: () => void }).onClick();
    expect(onChoose).toHaveBeenCalledOnce();
    expect(onChoose).toHaveBeenCalledWith(alternative, response);
  });
});

describe('QueryAnswer diagnostic entry', () => {
  it('is available for clarification without exposing the opaque continuation token', () => {
    const response = clarificationResponse([
      {
        candidate_id: 'opaque-do-not-render',
        kind: 'analysis_object',
        label: '获奖记录',
        description: '围绕获奖事实分析',
      },
    ]);

    const html = renderAnswer(response);

    expect(html).toContain('一键诊断');
    expect(html).not.toContain('opaque-do-not-render');
  });

  it('is available for a failed query without replacing the original error', () => {
    const response: AnalyticsFailedQueryResponse = {
      ...RESPONSE_BASE,
      state: 'FAILED',
      error: {
        stage: 'ROUTE_BINDING',
        code: 'ANALYSIS_TOPIC_MISSING',
        message: '没有可用的安全分析主题',
        retryable: false,
      },
    };

    const html = renderAnswer(response);

    expect(html).toContain('没有可用的安全分析主题');
    expect(html).toContain('一键诊断');
  });

  it('stays hidden while the answer is pending', () => {
    const html = renderToStaticMarkup(
      createElement(QueryAnswer, {
        projectId: 'project-1',
        turn: { id: 'turn-1', question: '处理中', pending: true },
        columnName: (id: string) => id,
        onChoose: vi.fn(),
      }),
    );

    expect(html).not.toContain('一键诊断');
  });
});
