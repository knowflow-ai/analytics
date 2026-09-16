/**
 * 画布上哪些字段要画连接点。
 *
 * 主标识可以拉新关系。已经有关系挂着的字段，哪怕后来被降成普通字段，也必须
 * 继续画连接点，只是不再允许从它拉新关系。否则 React Flow 找不到句柄会静默
 * 丢掉整条边，用户以为关系已经删了，目录里那条关系却还在，发布时被它拦下。
 */

interface RelationLike {
  readonly conditions: ReadonlyArray<{
    readonly left_field_id: string;
    readonly right_field_id: string;
  }>;
}

interface FieldLike {
  readonly id: string;
  readonly kind: string;
}

export type FieldHandleMode = 'connectable' | 'anchored';

export function relationAnchoredFieldIds(
  relations: ReadonlyArray<RelationLike>,
): ReadonlySet<string> {
  return new Set(
    relations.flatMap((relation) =>
      relation.conditions.flatMap((condition) => [
        condition.left_field_id,
        condition.right_field_id,
      ]),
    ),
  );
}

export function fieldHandleMode(
  field: FieldLike,
  anchored: ReadonlySet<string>,
): FieldHandleMode | null {
  if (field.kind === 'identifier') return 'connectable';
  if (anchored.has(field.id)) return 'anchored';
  return null;
}
