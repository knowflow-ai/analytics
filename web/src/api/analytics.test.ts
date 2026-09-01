import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AnalyticsSemanticQuery, AnalyticsTerm } from './types';

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock('./client', () => ({
  EDITION: 'oss',
  request: requestMock,
}));

import {
  deleteCatalogResource,
  grantProject,
  listProjectGrants,
  revokeProject,
  searchGrantSubjects,
  exportQueryDiagnostic,
  previewCatalogDeletion,
  previewQuery,
  previewStructuredQuery,
  query,
  saveTerm,
} from './analytics';

const version = {
  expected_etag: 8,
  schema_snapshot_hash: 'sha256:snapshot',
};
const term: AnalyticsTerm = {
  id: 'term-gmv',
  name: '成交额',
  description: '交易额',
  aliases: ['GMV'],
  dataset_ids: [],
  metric_ids: ['metric-gmv'],
  dimension_ids: [],
};

describe('business dictionary catalog requests', () => {
  beforeEach(() => requestMock.mockReset());

  it('saves the complete Term DTO with the current revision version', async () => {
    requestMock.mockResolvedValue({ id: 'revision-1', etag: 9 });

    await saveTerm('project-1', 'revision-1', version, term);

    expect(requestMock).toHaveBeenCalledWith(
      '/v1/analytics/projects/project-1/revisions/revision-1/catalog/terms/term-gmv',
      {
        method: 'PUT',
        projectId: 'project-1',
        body: { ...version, term },
      },
    );
  });

  it('encodes catalog resource ids as one URL path segment', async () => {
    requestMock.mockResolvedValueOnce({ impact_hash: 'sha256:impact', effects: [] });

    await previewCatalogDeletion(
      'project-1',
      'revision-1',
      version,
      'dimensions',
      'dimension:电商平台 ID',
    );

    expect(requestMock).toHaveBeenCalledWith(
      '/v1/analytics/projects/project-1/revisions/revision-1/catalog/dimensions/dimension%3A%E7%94%B5%E5%95%86%E5%B9%B3%E5%8F%B0%20ID/deletion-impact',
      {
        method: 'POST',
        projectId: 'project-1',
        body: version,
      },
    );
  });

  it.each(['.', '..', '../models/victim', 'safe\\..\\victim', 'bad\u0000id'])(
    'rejects unsafe catalog resource id %j before issuing a request',
    (resourceId) => {
      expect(() =>
        previewCatalogDeletion(
          'project-1',
          'revision-1',
          version,
          'metrics',
          resourceId,
        ),
      ).toThrow('catalog resource id contains an unsafe path segment');
      expect(requestMock).not.toHaveBeenCalled();
    },
  );

  it('deletes a term only after binding confirmation to the previewed impact hash', async () => {
    requestMock
      .mockResolvedValueOnce({ impact_hash: 'sha256:impact', effects: [] })
      .mockResolvedValueOnce({ id: 'revision-1', etag: 9 });

    await deleteCatalogResource(
      'project-1',
      'revision-1',
      version,
      'terms',
      'term-gmv',
    );

    const path =
      '/v1/analytics/projects/project-1/revisions/revision-1/catalog/terms/term-gmv';
    expect(requestMock).toHaveBeenNthCalledWith(1, `${path}/deletion-impact`, {
      method: 'POST',
      projectId: 'project-1',
      body: version,
    });
    expect(requestMock).toHaveBeenNthCalledWith(2, path, {
      method: 'DELETE',
      projectId: 'project-1',
      body: {
        ...version,
        expected_impact_hash: 'sha256:impact',
        confirmation: 'delete',
      },
    });
  });

  it('uses the exact impact hash already reviewed by the user without previewing again', async () => {
    requestMock.mockResolvedValueOnce({ id: 'revision-1', etag: 9 });

    await deleteCatalogResource(
      'project-1',
      'revision-1',
      version,
      'terms',
      'term-gmv',
      'sha256:reviewed-impact',
    );

    expect(requestMock).toHaveBeenCalledTimes(1);
    expect(requestMock).toHaveBeenCalledWith(
      '/v1/analytics/projects/project-1/revisions/revision-1/catalog/terms/term-gmv',
      {
        method: 'DELETE',
        projectId: 'project-1',
        body: {
          ...version,
          expected_impact_hash: 'sha256:reviewed-impact',
          confirmation: 'delete',
        },
      },
    );
  });
});

describe('query diagnostics requests', () => {
  beforeEach(() => requestMock.mockReset());

  it('requests the server-authored Markdown report inside the owning project', async () => {
    requestMock.mockResolvedValueOnce({ filename: 'diagnostic.md', timeline: [] });

    await exportQueryDiagnostic('project-1', 'query/a b');

    expect(requestMock).toHaveBeenCalledWith(
      '/v1/analytics/projects/project-1/query-diagnostics/export',
      { projectId: 'project-1', query: { query_id: 'query/a b' } },
    );
  });

  it('keeps ordinary Ask business-only while workbench previews request diagnostics', async () => {
    requestMock.mockResolvedValue({ state: 'COMPLETED' });
    const input = { question: '各地区销售额', dataset_ids: ['scope-orders'] };
    const semanticQuery: AnalyticsSemanticQuery = {
      dataset_id: 'scope-orders',
      query_type: 'aggregate',
      metric_ids: ['metric-revenue'],
      aggregation_overrides: [],
      dimension_ids: ['dimension-region'],
      filters: [],
      measure_filters: [],
      metric_filters: [],
      order_by: [],
      limit: null,
    };

    await query('project-1', input);
    await previewQuery('project-1', 'revision-1', version, input);
    await previewStructuredQuery('project-1', 'revision-1', version, semanticQuery);

    expect(requestMock).toHaveBeenNthCalledWith(1, '/v1/analytics/query', {
      method: 'POST',
      projectId: 'project-1',
      body: {
        project_id: 'project-1',
        ...input,
        include_diagnostics: false,
        include_debug_sql: false,
      },
    });
    expect(requestMock).toHaveBeenNthCalledWith(
      2,
      '/v1/analytics/projects/project-1/revisions/revision-1/query-preview',
      {
        method: 'POST',
        projectId: 'project-1',
        body: {
          ...version,
          ...input,
          include_diagnostics: true,
          include_debug_sql: true,
        },
      },
    );
    expect(requestMock).toHaveBeenNthCalledWith(
      3,
      '/v1/analytics/projects/project-1/revisions/revision-1/structured-query-preview',
      {
        method: 'POST',
        projectId: 'project-1',
        body: {
          ...version,
          semantic_query: semanticQuery,
          include_debug_sql: true,
        },
      },
    );
  });
});

describe('问数项目授权（仅嵌入版可见，接口走宿主路径）', () => {
  beforeEach(() => requestMock.mockReset());

  it('授权与撤销打到宿主的转发路由，而不是核心', async () => {
    requestMock.mockResolvedValue(true);
    await grantProject('prj_1', {
      subject_type: 'user',
      subject_id: 'u-2',
      role_code: 'viewer',
    });

    const [path, options] = requestMock.mock.calls[0];
    // 核心不认识授权：授权是宿主（商业版）能力，路径不带 /v1/analytics 前缀，
    // 因此不会被 client 的 rewritePath 改写到核心直通道上。
    expect(path).toBe('/v1/kb_folder/analytics_project_grant');
    expect(options.method).toBe('POST');
    expect(options.body).toEqual({
      project_id: 'prj_1',
      subject_type: 'user',
      subject_id: 'u-2',
      role_code: 'viewer',
    });

    await revokeProject('prj_1', {
      subject_type: 'org',
      subject_id: 'org-1',
      role_code: 'viewer',
    });
    expect(requestMock.mock.calls[1][0]).toBe('/v1/kb_folder/analytics_project_revoke');
  });

  it('项目 id 进 query 时被编码，列授权只读', async () => {
    requestMock.mockResolvedValue({ users: [], orgs: [], groups: [] });
    await listProjectGrants('prj/1');

    const [path, options] = requestMock.mock.calls[0];
    expect(path).toBe('/v1/kb_folder/analytics_project_grants?project_id=prj%2F1');
    expect(options).toBeUndefined();
  });

  it('三类主体各自的检索路径', async () => {
    requestMock.mockResolvedValue([]);
    await searchGrantSubjects('user', '张');
    await searchGrantSubjects('org', '');
    await searchGrantSubjects('group', 'a b');

    expect(requestMock.mock.calls.map((call) => call[0])).toEqual([
      '/v1/kb_folder/subjects/users?keyword=%E5%BC%A0',
      '/v1/kb_folder/subjects/orgs',
      '/v1/kb_folder/subjects/groups?keyword=a%20b',
    ]);
  });
});
