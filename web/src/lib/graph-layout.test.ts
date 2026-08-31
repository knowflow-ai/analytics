import { describe, expect, it } from 'vitest';
import {
  nodeHeightForFieldCount,
  packedFallbackPositions,
  unplacedModelIds,
} from './graph-layout';

const models = (...ids: string[]) => ids.map((id) => ({ id }) as never);

describe('unplacedModelIds', () => {
  it('导入新表后,新表就是没有坐标的那些', () => {
    expect(
      unplacedModelIds(models('orders', 'customers', 'shops'), new Set(['orders'])),
    ).toEqual(['customers', 'shops']);
  });

  it('全部排过就不再自动整理,不覆盖用户拖过的位置', () => {
    expect(unplacedModelIds(models('orders'), new Set(['orders']))).toEqual([]);
  });
});

describe('packedFallbackPositions', () => {
  /** 兜底格只在 ELK 算完前的一瞬间可见,但它也不能压盖。 */
  const overlapCount = (
    positions: Record<string, { x: number; y: number }>,
    counts: Map<string, number>,
  ) => {
    const boxes = Object.entries(positions).map(([id, p]) => ({
      x: p.x,
      y: p.y,
      w: 252,
      h: nodeHeightForFieldCount(counts.get(id) ?? 0),
    }));
    let n = 0;
    for (let i = 0; i < boxes.length; i += 1)
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i];
        const b = boxes[j];
        if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) n += 1;
      }
    return n;
  };

  it('高矮不齐的节点也不重叠——固定行距正是旧占位格压盖的原因', () => {
    const ids = ['a', 'b', 'c', 'd', 'e', 'f', 'g'];
    // 字段数差异极大:1 列到 40 列
    const counts = new Map(ids.map((id, index) => [id, [1, 40, 3, 25, 2, 12, 8][index]]));
    const positions = packedFallbackPositions({ models: models(...ids), fieldCounts: counts });
    expect(Object.keys(positions)).toHaveLength(ids.length);
    expect(overlapCount(positions, counts)).toBe(0);
  });

  it('单个模型落在原点', () => {
    expect(packedFallbackPositions({ models: models('a'), fieldCounts: new Map([['a', 3]]) })).toEqual({
      a: { x: 0, y: 0 },
    });
  });

  it('没有模型时返回空,不抛错', () => {
    expect(packedFallbackPositions({ models: [], fieldCounts: new Map() })).toEqual({});
  });
});
