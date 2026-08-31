import type {
  AnalyticsCatalogMetric,
  AnalyticsField,
  AnalyticsRelation,
} from '@analytics/api/types';

/**
 * Deletion previews are asynchronous and bind a later destructive action.
 * This gate keeps them single-flight and makes responses from a cancelled or
 * superseded request ineligible to open a confirmation dialog.
 */
export class DeletionPreviewGuard {
  private generation = 0;
  private inFlight = false;

  get active() {
    return this.inFlight;
  }

  begin(): number | null {
    if (this.inFlight) return null;
    this.inFlight = true;
    this.generation += 1;
    return this.generation;
  }

  settle(generation: number): boolean {
    if (!this.inFlight || generation !== this.generation) return false;
    this.inFlight = false;
    return true;
  }

  cancel() {
    this.generation += 1;
    this.inFlight = false;
  }
}

/**
 * 字段角色什么时候锁——跟着服务端的实际行为走，不自己发明第二套哲学。
 *
 * 用真实 catalog 对编译器做过的实验（rev_3eec）：
 * - 目录维度按物理列(expr)解析，字段角色怎么改列都在 → 维度/层级/查询作用域全不受影响；
 * - 关系 join 按列编译，不校验角色 → 改了照样能存(治理约定破了,提示即可)；
 * - 唯一会被服务端拒绝的：度量改成别的角色时，MEASURE 型指标的口径还引用着
 *   该 model measure（catalog_compiler "references an unknown model measure"）。
 * 所以只有最后一种是锁，且必须指名道姓是哪个指标在挡。
 */
export function roleChangeBlockers(
  field: Pick<AnalyticsField, 'model_id' | 'column' | 'kind'>,
  metrics: readonly AnalyticsCatalogMetric[],
): AnalyticsCatalogMetric[] {
  // 只有"度量→别的角色"会删掉 model measure；其它角色改动不删任何被引用物。
  if (field.kind !== 'measure') return [];
  const column = field.column.toLowerCase();
  return metrics.filter(
    (metric) =>
      metric.modelId === field.model_id &&
      metric.metricDefineType === 'MEASURE' &&
      (metric.metricDefineByMeasureParams?.measures ?? []).some(
        // 编译器按 bizName casefold 匹配 model measure，这里必须一致。
        (measure) => measure.bizName.toLowerCase() === column,
      ),
  );
}

/** 关系 join 引用该字段：可改，但用户该知道有这层关系。 */
export function relationReferences(
  field: Pick<AnalyticsField, 'id'>,
  relations: readonly AnalyticsRelation[],
): AnalyticsRelation[] {
  return relations.filter((relation) =>
    relation.conditions.some(
      (c) => c.left_field_id === field.id || c.right_field_id === field.id,
    ),
  );
}

// --- 删除影响 ----------------------------------------------------------------

/** 服务端 deletion-impact 返回的规范化影响项。 */
export interface DeletionEffect {
  action: string;
  resource_kind: string;
  resource_id: string;
  reason?: string;
}

export interface DeletionResourceRef {
  resource_kind: string;
  resource_id: string;
}

export function deletionResourceKey(resourceKind: string, resourceId: string): string {
  return `${resourceKind}:${resourceId}`;
}

const KIND_LABELS: Record<string, string> = {
  dimension: '维度',
  metric: '指标',
  relation: '关系',
  model: '实体',
  hierarchy: '层级',
  term: '术语',
  dataset: '查询作用域',
  query_rule: '查询规则',
};

/**
 * 把服务端的级联影响翻译成确认框里的人话。
 * 服务端 CatalogDeletionPlanner 早就会级联(维度值、查询作用域 unlink、依赖指标),
 * 前端一直拿到 effects 却没给用户看——删除是安全的,只是不告知。
 */
export function describeDeletionEffects(
  effects: readonly DeletionEffect[],
  deleted: DeletionResourceRef,
  namesByResource: ReadonlyMap<string, string>,
): string[] {
  const lines: string[] = [];
  let dimensionValues = 0;
  let semanticContexts = 0;
  for (const effect of effects) {
    if (
      effect.resource_kind === deleted.resource_kind &&
      effect.resource_id === deleted.resource_id
    ) continue; // 只有同类型、同 ID 的删除对象本身不算连带
    if (effect.resource_kind === 'dimension_value') {
      dimensionValues += 1;
      continue;
    }
    if (effect.resource_kind === 'semantic_context') {
      semanticContexts += 1;
      continue;
    }
    const name = namesByResource.get(
      deletionResourceKey(effect.resource_kind, effect.resource_id),
    ) ?? effect.resource_id;
    if (effect.action === 'unlink') {
      const label = effect.resource_kind === 'dataset' ? `从查询作用域「${name}」解除成员引用` : `从${KIND_LABELS[effect.resource_kind] ?? effect.resource_kind}「${name}」解除引用`;
      lines.push(label);
    } else {
      const kind = KIND_LABELS[effect.resource_kind] ?? effect.resource_kind;
      lines.push(`连带删除${kind}「${name}」${effect.reason ? `(${effect.reason})` : ''}`);
    }
  }
  if (dimensionValues > 0) lines.push(`删除 ${dimensionValues} 条维度值别名`);
  if (semanticContexts > 0) lines.push(`删除 ${semanticContexts} 条语义上下文`);
  return lines;
}
