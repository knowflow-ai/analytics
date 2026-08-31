import { describe, expect, it } from 'vitest';
import type { AnalyticsField } from '@analytics/api/types';
import { describeError, inferCardinality } from './labels';

const field = (id: string, identifier_type: 'primary' | 'foreign' | null): AnalyticsField => ({
  id,
  model_id: 'm',
  name: id,
  column: id,
  data_type: 'int',
  kind: identifier_type ? 'identifier' : 'field',
  identifier_type,
  dimension_type: null,
  semantic_expr: null,
  unit: null,
  default_aggregation: null,
  description: '',
  aliases: [],
  nullable: false,
  create_dimension: false,
  create_metric: false,
});

describe('inferCardinality', () => {
  const fields = [field('pk', 'primary'), field('fk', 'foreign'), field('plain', null)];
  it('reads foreign → primary as many-to-one', () => {
    expect(inferCardinality({ conditions: [{ left_field_id: 'fk', right_field_id: 'pk' }] }, fields)).toBe('many_to_one');
  });
  it('reads primary → foreign as one-to-many', () => {
    expect(inferCardinality({ conditions: [{ left_field_id: 'pk', right_field_id: 'fk' }] }, fields)).toBe('one_to_many');
  });
  it('falls back to many-to-many when roles are unknown or missing', () => {
    expect(inferCardinality({ conditions: [{ left_field_id: 'plain', right_field_id: 'pk' }] }, fields)).toBe('many_to_many');
    expect(inferCardinality({ conditions: [] }, fields)).toBe('many_to_many');
  });
});

describe('describeError', () => {
  it('explains the unconfigured service', () => {
    expect(describeError({ message: 'not_configured', code: 'not_configured', status: 503 })).toContain('设置');
  });
  it('appends business codes once', () => {
    expect(describeError({ message: 'stale', code: 'REVISION_CONFLICT' })).toBe('stale（REVISION_CONFLICT）');
    expect(describeError({ message: 'x', code: 'x' })).toBe('x');
  });
});
