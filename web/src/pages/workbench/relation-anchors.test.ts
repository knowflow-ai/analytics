import { describe, expect, it } from 'vitest';
import { fieldHandleMode, relationAnchoredFieldIds } from './relation-anchors';

const relations = [
  {
    id: 'relation_scores_warnings',
    left_model_id: 'model_scores',
    right_model_id: 'model_warnings',
    join_type: 'inner',
    cardinality: 'one_to_many',
    conditions: [
      {
        left_field_id: 'field_scores_acct',
        right_field_id: 'field_warnings_zhhao',
      },
    ],
  },
] as const;

describe('画布上哪些字段有连接点', () => {
  it('主标识可以拉新关系', () => {
    const anchored = relationAnchoredFieldIds(relations);
    expect(fieldHandleMode({ id: 'field_scores_zhhao', kind: 'identifier' }, anchored)).toBe(
      'connectable',
    );
  });

  it('已有关系挂着的普通字段保留连接点，连线不会因为降级而消失', () => {
    // 现场：把 acct 从主标识降成普通字段后，关系还在目录里，画布却没了连线，
    // 用户以为已经删掉，发布时被同一条关系拦下。
    const anchored = relationAnchoredFieldIds(relations);
    expect(fieldHandleMode({ id: 'field_scores_acct', kind: 'field' }, anchored)).toBe('anchored');
  });

  it('没关系挂着的普通字段没有连接点', () => {
    const anchored = relationAnchoredFieldIds(relations);
    expect(fieldHandleMode({ id: 'field_scores_cjrq', kind: 'dimension' }, anchored)).toBeNull();
  });
});
