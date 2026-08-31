import { afterEach, describe, expect, it, vi } from 'vitest';

import { request } from './client';

describe('analytics API error envelopes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('preserves a structured FastAPI detail code and message', async () => {
    vi.stubGlobal('window', {
      location: { origin: 'http://localhost:9395', assign: vi.fn() },
      localStorage: { getItem: vi.fn(() => ''), removeItem: vi.fn() },
      dispatchEvent: vi.fn(),
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              code: 'QUERY_DIAGNOSTIC_NOT_FOUND',
              message: 'query diagnostic was not found',
            },
          }),
          { status: 404, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );

    await expect(
      request('/v1/analytics/projects/project-1/query-diagnostics/export', {
        projectId: 'project-1',
        query: { query_id: 'q_missing' },
      }),
    ).rejects.toMatchObject({
      status: 404,
      code: 'QUERY_DIAGNOSTIC_NOT_FOUND',
      message: 'query diagnostic was not found',
    });
  });
});
