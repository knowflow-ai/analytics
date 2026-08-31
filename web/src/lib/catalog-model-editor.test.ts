import { describe, expect, it } from 'vitest';

import type { AnalyticsCatalogModel } from '@analytics/api/types';
import { updateCatalogModelFieldRole } from './catalog-model-editor';

const model = {
  id: 'model-orders',
  name: '订单',
  bizName: 'orders',
  description: '',
  sensitiveLevel: 0,
  modelDetail: {
    queryType: 'table_query',
    tableQuery: 'sales.orders',
    identifiers: [],
    dimensions: [
      {
        name: '区域',
        type: 'categorical',
        expr: 'region',
        dateFormat: 'yyyy-MM-dd',
        dataType: 'text',
        typeParams: null,
        isCreateDimension: 1,
        bizName: 'region',
        description: '已审核的独立维度说明',
      },
    ],
    measures: [],
    fields: [{ fieldName: 'region', dataType: 'text' }],
    sqlVariables: [],
  },
  viewers: [],
  viewOrgs: [],
  admins: [],
  adminOrgs: [],
  ext: {},
} as AnalyticsCatalogModel;

describe('Catalog field role editor', () => {
  it('preserves the governed dimension description when raw field metadata is saved', () => {
    const updated = updateCatalogModelFieldRole(model, 'region', {
      name: '销售区域',
      kind: 'dimension',
      dimensionType: 'categorical',
      createDimension: true,
    });

    expect(updated.modelDetail.dimensions[0].description).toBe(
      '已审核的独立维度说明',
    );
  });
});
