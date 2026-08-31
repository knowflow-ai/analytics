import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { AnalyticsRevision } from '@analytics/api/types';
import {
  CATALOG_AI_ACTION,
  CATALOG_NAV_ITEMS,
  SemanticCatalogPanel,
  catalogAiActionState,
  catalogResourceDestination,
  initialCatalogView,
  semanticContextSourceLabel,
} from './semantic-catalog-panel';
import {
  hasReviewableModelingProposal,
  isCurrentModelingJob,
  modelingJobStorageKey,
} from './ai-modeling';

describe('semanticContextSourceLabel', () => {
  it('labels generated catalog prose without pretending it is a human convention', () => {
    expect(semanticContextSourceLabel('catalog_description')).toBe('目录说明');
  });
});

describe('semantic modeling navigation', () => {
  it('keeps recurring catalog work in navigation and actions out of it', () => {
    expect(CATALOG_NAV_ITEMS.map((item) => item.label)).toEqual([
      '实体与关系',
      '业务词典',
      '目录概览',
    ]);
    expect(CATALOG_AI_ACTION).toEqual({
      label: 'AI 一键建模',
      variant: 'primary',
    });
  });

  it('opens first-time empty modeling in AI while returning established catalogs to overview', () => {
    expect(initialCatalogView({ metrics: [], dimensions: [] })).toBe('ai');
    expect(initialCatalogView({ metrics: [{ id: 'm' }], dimensions: [] })).toBe('overview');
  });

  it('deep-links language resources from the catalog inventory into the business dictionary', () => {
    expect(catalogResourceDestination('terms')).toEqual({
      view: 'dictionary',
      section: 'terms',
    });
    expect(catalogResourceDestination('dimensionValues')).toEqual({
      view: 'dictionary',
      section: 'dimensionValues',
    });
    expect(catalogResourceDestination('metrics')).toEqual({ view: 'overview' });
  });

  it('lets users enter proposal review from another catalog view', () => {
    expect(
      catalogAiActionState({
        view: 'overview',
        modelingRunning: false,
        modelingReady: true,
      }),
    ).toEqual({
      label: '审核 AI 建议',
      disabled: false,
      current: false,
    });
  });

  it('marks proposal review as the current destination instead of exposing a no-op button', () => {
    expect(
      catalogAiActionState({
        view: 'ai',
        modelingRunning: false,
        modelingReady: true,
      }),
    ).toEqual({
      label: '正在审核',
      disabled: true,
      current: true,
    });
  });

  it('keeps the current AI modeling destination visibly active while work is running', () => {
    expect(
      catalogAiActionState({
        view: 'ai',
        modelingRunning: true,
        modelingReady: false,
      }),
    ).toEqual({
      label: 'AI 建模中',
      disabled: true,
      current: true,
    });
  });

  it('keeps the AI job bound to the current revision while users inspect other views', () => {
    expect(modelingJobStorageKey('revision-8')).toBe(
      'knowflow-analytics.modeling-job.revision-8',
    );
    expect(isCurrentModelingJob({ revision_etag: 8 }, 8)).toBe(true);
    expect(isCurrentModelingJob({ revision_etag: 7 }, 8)).toBe(false);
    expect(
      hasReviewableModelingProposal(
        { revision_etag: 8, status: 'completed' },
        { status: 'draft' },
        8,
      ),
    ).toBe(true);
    expect(
      hasReviewableModelingProposal(
        { revision_etag: 8, status: 'completed' },
        { status: 'applied' },
        8,
      ),
    ).toBe(false);
    expect(
      hasReviewableModelingProposal(
        { revision_etag: 8, status: 'completed' },
        undefined,
        8,
      ),
    ).toBe(false);
  });

  it('renders actions separately from the three recurring catalog destinations', () => {
    const revision = {
      id: 'revision-1',
      project_id: 'project-1',
      etag: 1,
      state: 'draft',
      schema_snapshot_hash: 'sha256:snapshot',
      semantic_spec: {
        models: [],
        fields: [],
        relations: [],
        dimensions: [],
        metrics: [{ id: 'metric-gmv', name: '交易额', model_id: 'model-orders' }],
        datasets: [],
      },
      semantic_catalog: {
        projectId: 'project-1',
        revisionId: 'revision-1',
        contractVersion: 'knowflow-modeling-v1',
        models: [],
        modelRelations: [],
        dimensions: [],
        metrics: [],
        dataSets: [],
        terms: [],
        dimensionValues: [],
      },
    } as unknown as AnalyticsRevision;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const html = renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(SemanticCatalogPanel, {
          projectId: 'project-1',
          revision,
          acceptRevision: () => undefined,
          readOnly: false,
          goTo: () => undefined,
        }),
      ),
    );

    expect(html).toContain('实体与关系');
    expect(html).toContain('业务词典');
    expect(html).toContain('目录概览');
    expect(html).toContain('AI 一键建模');
    expect(html).toContain('高级诊断');
  });
});
