import { describe, expect, it } from 'vitest';

import {
  POSTGRES_CONNECTION_HINT,
  POSTGRES_CONNECTION_PLACEHOLDER,
} from './settings';

describe('PostgreSQL connection help', () => {
  it('shows the canonical psycopg 3 URL and explains accepted shorthand forms', () => {
    expect(POSTGRES_CONNECTION_PLACEHOLDER).toBe(
      'postgresql+psycopg://user:password@host:5432/database',
    );
    expect(POSTGRES_CONNECTION_HINT).toContain('psycopg 3');
    expect(POSTGRES_CONNECTION_HINT).toContain('postgresql://');
    expect(POSTGRES_CONNECTION_HINT).toContain('自动转换');
    expect(POSTGRES_CONNECTION_HINT).toContain('host.docker.internal');
  });
});
