/**
 * 实体编辑器右栏（详情面板）显示什么。
 *
 * 原来 4 个 editingXxx 状态各管一段 UI，编辑器被追加到弹窗正文末尾；
 * 这里收敛成「一个选中态 → 一个详情」，让详情面板成为唯一的编辑落点。
 */
export type EntitySelection =
  | { kind: 'field'; id: string }
  | { kind: 'dimension'; id: string }
  | { kind: 'metric'; id: string }
  | { kind: 'hierarchy'; id: string }
  | { kind: 'new-hierarchy' }
  | null;

interface WithId {
  id: string;
}

interface Catalogs<F extends WithId, D extends WithId, M extends WithId, H extends WithId> {
  fields: readonly F[];
  dimensions: readonly D[];
  metrics: readonly M[];
  hierarchies: readonly H[];
}

export type EntityDetail<F, D, M, H> =
  | { kind: 'overview' }
  | { kind: 'field'; field: F }
  | { kind: 'dimension'; dimension: D }
  | { kind: 'metric'; metric: M }
  /** hierarchy 为 null 表示新建。 */
  | { kind: 'hierarchy'; hierarchy: H | null }
  | { kind: 'missing'; message: string };

const MISSING: Record<string, string> = {
  field: '目录里找不到该字段，请刷新。',
  dimension: '目录里找不到该维度，请刷新。',
  metric: '目录里找不到该指标，请刷新。',
  hierarchy: '目录里找不到该层级，请刷新。',
};

export function resolveEntityDetail<F extends WithId, D extends WithId, M extends WithId, H extends WithId>(
  selection: EntitySelection,
  catalogs: Catalogs<F, D, M, H>,
): EntityDetail<F, D, M, H> {
  if (!selection) return { kind: 'overview' };
  if (selection.kind === 'new-hierarchy') return { kind: 'hierarchy', hierarchy: null };

  // 保存或删除后目录会整体换新，旧选中态可能已经指向不存在的对象。
  const missing = { kind: 'missing', message: MISSING[selection.kind] } as const;
  switch (selection.kind) {
    case 'field': {
      const field = catalogs.fields.find((f) => f.id === selection.id);
      return field ? { kind: 'field', field } : missing;
    }
    case 'dimension': {
      const dimension = catalogs.dimensions.find((d) => d.id === selection.id);
      return dimension ? { kind: 'dimension', dimension } : missing;
    }
    case 'metric': {
      const metric = catalogs.metrics.find((m) => m.id === selection.id);
      return metric ? { kind: 'metric', metric } : missing;
    }
    default: {
      const hierarchy = catalogs.hierarchies.find((h) => h.id === selection.id);
      return hierarchy ? { kind: 'hierarchy', hierarchy } : missing;
    }
  }
}

/**
 * 维度和指标都是从字段派生出来的，但列表把它们和字段表上下分开摆，
 * 因果看不出来。编辑字段时把它的派生物一并显示。
 */
export function derivedFromField<
  D extends { field_id: string | null },
  M extends { field_id: string | null },
>(fieldId: string, spec: { dimensions: readonly D[]; metrics: readonly M[] }) {
  return {
    dimensions: spec.dimensions.filter((d) => d.field_id === fieldId),
    metrics: spec.metrics.filter((m) => m.field_id === fieldId),
  };
}
