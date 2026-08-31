import { describe, expect, it } from 'vitest';

import { derivedFromField, resolveEntityDetail } from './entity-detail';

const catalogs = {
  fields: [{ id: 'f1' }, { id: 'f2' }],
  dimensions: [{ id: 'd1' }, { id: 'd2' }],
  metrics: [{ id: 'm1' }],
  hierarchies: [{ id: 'h1' }],
};

describe('详情面板要显示什么', () => {
  it('没有选中时显示派生概览', () => {
    expect(resolveEntityDetail(null, catalogs)).toEqual({ kind: 'overview' });
  });

  it('选中字段时显示该字段', () => {
    expect(resolveEntityDetail({ kind: 'field', id: 'f2' }, catalogs)).toEqual({
      kind: 'field',
      field: { id: 'f2' },
    });
  });

  it('选中的对象刚被删掉时提示刷新,而不是白屏', () => {
    const detail = resolveEntityDetail({ kind: 'dimension', id: 'gone' }, catalogs);
    expect(detail.kind).toBe('missing');
    if (detail.kind === 'missing') expect(detail.message).toContain('刷新');
  });

  it('维度/指标/层级各自从对应目录里取', () => {
    expect(resolveEntityDetail({ kind: 'dimension', id: 'd1' }, catalogs)).toEqual({
      kind: 'dimension',
      dimension: { id: 'd1' },
    });
    expect(resolveEntityDetail({ kind: 'metric', id: 'm1' }, catalogs)).toEqual({
      kind: 'metric',
      metric: { id: 'm1' },
    });
    expect(resolveEntityDetail({ kind: 'hierarchy', id: 'h1' }, catalogs)).toEqual({
      kind: 'hierarchy',
      hierarchy: { id: 'h1' },
    });
  });

  it('新建层级是没有既有对象的层级详情', () => {
    expect(resolveEntityDetail({ kind: 'new-hierarchy' }, catalogs)).toEqual({
      kind: 'hierarchy',
      hierarchy: null,
    });
  });

  it('字段被删后选中态不会串到别的字段', () => {
    const detail = resolveEntityDetail({ kind: 'field', id: 'f9' }, catalogs);
    expect(detail.kind).toBe('missing');
  });
});

describe('字段派生了什么', () => {
  const spec = {
    dimensions: [
      { id: 'd1', field_id: 'f1', name: '地区' },
      { id: 'd2', field_id: 'f2', name: '渠道' },
    ],
    metrics: [
      { id: 'm1', field_id: 'f1', name: '净金额' },
      { id: 'm2', field_id: null, name: '订单数量' },
    ],
  };

  it('列出该字段派生出的维度与指标', () => {
    expect(derivedFromField('f1', spec)).toEqual({
      dimensions: [{ id: 'd1', field_id: 'f1', name: '地区' }],
      metrics: [{ id: 'm1', field_id: 'f1', name: '净金额' }],
    });
  });

  it('没有派生物时返回空,不返回 undefined', () => {
    expect(derivedFromField('f-none', spec)).toEqual({ dimensions: [], metrics: [] });
  });

  it('不把 field_id 为空的指标算到任何字段名下', () => {
    const all = ['f1', 'f2', 'f-none'].flatMap((id) => derivedFromField(id, spec).metrics);
    expect(all.map((m) => m.id)).not.toContain('m2');
  });
});
