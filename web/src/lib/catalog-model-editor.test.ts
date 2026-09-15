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

describe('Plain field display name', () => {
  const plain = {
    ...model,
    modelDetail: { ...model.modelDetail, dimensions: [], fields: [{ fieldName: 'region', dataType: 'text' }] },
  } as AnalyticsCatalogModel;

  it('persists a renamed plain field into the physical field entry', () => {
    // 客户实机（knowflow-ai/analytics#2）：无角色字段改名提示已保存，名字却没变。
    // 目录里普通字段此前没有业务名存储位，名字在请求发出前就被丢掉。
    const next = updateCatalogModelFieldRole(plain, 'region', { name: '区域编码', kind: 'field' });

    expect(next.modelDetail.fields).toEqual([{ fieldName: 'region', dataType: 'text', name: '区域编码' }]);
    expect(next.modelDetail.dimensions).toEqual([]);
  });

  it('clears the display name when it equals the physical column again', () => {
    const named = updateCatalogModelFieldRole(plain, 'region', { name: '区域编码', kind: 'field' });
    const reverted = updateCatalogModelFieldRole(named, 'region', { name: 'region', kind: 'field' });

    expect(reverted.modelDetail.fields).toEqual([{ fieldName: 'region', dataType: 'text', name: null }]);
  });

  it('leaves the physical entry alone when the field is given a role', () => {
    const next = updateCatalogModelFieldRole(plain, 'region', { name: '区域', kind: 'dimension' });

    expect(next.modelDetail.fields).toEqual([{ fieldName: 'region', dataType: 'text' }]);
    expect(next.modelDetail.dimensions[0].name).toBe('区域');
  });
});
