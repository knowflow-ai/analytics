import type {
  AnalyticsModel,
  AnalyticsRelation,
} from '@analytics/api/types';
import ELK from 'elkjs/lib/elk.bundled.js';

/** 与 AnalyticsGraphModelNode 的渲染保持一致，否则 ELK 会按错误尺寸排布。 */
export const NODE_WIDTH = 252;
const NODE_HEADER = 74;
const NODE_ROW = 22;
const NODE_FOOTER = 30;

export function nodeHeightForFieldCount(fieldCount: number) {
  return NODE_HEADER + fieldCount * NODE_ROW + NODE_FOOTER;
}

export interface GraphPosition {
  x: number;
  y: number;
}

/**
 * ELK 的分层布局参数，与 Dify 的工作流画布同源。
 *
 * 关键是后两项：LAYER_SWEEP 在同层内反复扫描调整顺序以最小化边交叉，
 * BRANDES_KOEPF 在节点高度不一致时给出更整齐的对齐 —— 本模块的节点高度
 * 随字段数从 ~100px 变化到 ~400px，这两点都是手写分层做不到的。
 */
const LAYOUT_OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.layered.spacing.nodeNodeBetweenLayers': '120',
  'elk.spacing.nodeNode': '80',
  'elk.spacing.edgeNode': '50',
  'elk.spacing.edgeEdge': '30',
  'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
  'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
};

const elk = new ELK();

/**
 * 按关系拓扑重排实体，让连线尽量从左往右且不交叉。
 *
 * 之前是手写 BFS 分层：只做了"按层分列 + 同列累加高度"，完全没有做同层内的
 * 顺序优化，于是指向同一实体的多条边会挤在一起。交叉最小化是分层布局里最难
 * 的一步，交给 ELK 做。
 */
export async function tidyLayout({
  models,
  relations,
  fieldCounts,
}: {
  models: readonly AnalyticsModel[];
  relations: readonly Pick<
    AnalyticsRelation,
    'left_model_id' | 'right_model_id'
  >[];
  fieldCounts: ReadonlyMap<string, number>;
}): Promise<Record<string, GraphPosition>> {
  const known = new Set(models.map((item) => item.id));

  const graph = {
    id: 'root',
    layoutOptions: LAYOUT_OPTIONS,
    children: models.map((model) => ({
      id: model.id,
      width: NODE_WIDTH,
      height: nodeHeightForFieldCount(fieldCounts.get(model.id) ?? 0),
    })),
    edges: relations
      // 指向画布外模型的关系会让 ELK 报错，先过滤掉。
      .filter(
        (relation) =>
          known.has(relation.left_model_id) &&
          known.has(relation.right_model_id) &&
          relation.left_model_id !== relation.right_model_id,
      )
      .map((relation, index) => ({
        id: `edge-${index}`,
        sources: [relation.left_model_id],
        targets: [relation.right_model_id],
      })),
  };

  const laid = await elk.layout(graph);
  const positions: Record<string, GraphPosition> = {};
  (laid.children ?? []).forEach((child) => {
    positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  });
  // ELK 理论上会给每个 child 返回坐标；万一缺失就退回原点，
  // 至少不让节点从画布上消失。
  models.forEach((model) => {
    if (!positions[model.id]) positions[model.id] = { x: 0, y: 0 };
  });
  return positions;
}

/**
 * 还没有坐标的模型——导入新表后就是这些。
 *
 * 服务端读取路径只回答"已经排好的在哪里",缺坐标就是缺坐标;判断为空才说明
 * 整张图都排过,不该再覆盖用户拖出来的位置。
 */
export function unplacedModelIds(
  models: readonly Pick<AnalyticsModel, 'id'>[],
  placed: ReadonlySet<string>,
): string[] {
  return models.filter((model) => !placed.has(model.id)).map((model) => model.id);
}

/**
 * ELK 算完前那一瞬间的兜底坐标。
 *
 * 按列打包并累加各自的真实节点高度——旧的固定行距占位格正是节点压盖的原因:
 * 行距写死 230px,而节点高随字段数从 ~130px 长到 ~400px。
 */
export function packedFallbackPositions({
  models,
  fieldCounts,
  columns = 3,
  gapX = 88,
  gapY = 40,
}: {
  models: readonly Pick<AnalyticsModel, 'id'>[];
  fieldCounts: ReadonlyMap<string, number>;
  columns?: number;
  gapX?: number;
  gapY?: number;
}): Record<string, GraphPosition> {
  const nextY = new Array<number>(columns).fill(0);
  const positions: Record<string, GraphPosition> = {};
  models.forEach((model, index) => {
    const column = index % columns;
    positions[model.id] = { x: column * (NODE_WIDTH + gapX), y: nextY[column] };
    nextY[column] += nodeHeightForFieldCount(fieldCounts.get(model.id) ?? 0) + gapY;
  });
  return positions;
}
