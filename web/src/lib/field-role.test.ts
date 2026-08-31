import { describe, expect, it } from 'vitest';

import { FIELD_ROLE_TEXT_CLASS, fieldRoleVisual } from './field-role';

const primary = { kind: 'identifier', identifier_type: 'primary' } as const;
const foreign = { kind: 'identifier', identifier_type: 'foreign' } as const;
const dimension = { kind: 'dimension', identifier_type: null } as const;
const time = { kind: 'time', identifier_type: null } as const;
const measure = { kind: 'measure', identifier_type: null } as const;
const unset = { kind: 'field', identifier_type: null } as const;

describe('字段角色的视觉映射', () => {
  it('标识族同色相，主标识实心、外部标识描边', () => {
    const p = fieldRoleVisual(primary);
    const f = fieldRoleVisual(foreign);
    expect(p.tone).toBe(f.tone);
    expect(p.variant).toBe('solid');
    expect(f.variant).toBe('outline');
  });

  it('切分族同色相，时间靠图标而不是再开一个色相', () => {
    const d = fieldRoleVisual(dimension);
    const t = fieldRoleVisual(time);
    expect(d.tone).toBe(t.tone);
    expect(d.icon).toBeNull();
    expect(t.icon).toBe('clock');
  });

  it('标识族与切分族色相不同，能一眼分开', () => {
    expect(fieldRoleVisual(primary).tone).not.toBe(fieldRoleVisual(dimension).tone);
  });

  it('度量单独一个色相', () => {
    const m = fieldRoleVisual(measure).tone;
    expect(m).not.toBe(fieldRoleVisual(primary).tone);
    expect(m).not.toBe(fieldRoleVisual(dimension).tone);
  });

  it('待确认用 amber 提示，不再占用 blue', () => {
    expect(fieldRoleVisual(unset).tone).toBe('amber');
  });

  it('blue 只留给操作:任何角色都不得使用', () => {
    for (const field of [primary, foreign, dimension, time, measure, unset]) {
      expect(fieldRoleVisual(field).tone).not.toBe('blue');
    }
  });

  it('六个角色至多两两同色，不存在四个角色挤在同一个灰', () => {
    const tones = [primary, foreign, dimension, time, measure, unset].map(
      (f) => `${fieldRoleVisual(f).tone}/${fieldRoleVisual(f).variant}/${fieldRoleVisual(f).icon ?? ''}`,
    );
    // 每个角色的「色+变体+图标」组合必须唯一，否则扫不出结构
    expect(new Set(tones).size).toBe(6);
  });

  it('沿用既有中文角色名', () => {
    expect(fieldRoleVisual(primary).label).toBe('主标识');
    expect(fieldRoleVisual(foreign).label).toBe('外部标识');
    expect(fieldRoleVisual(dimension).label).toBe('维度');
    expect(fieldRoleVisual(time).label).toBe('时间');
    expect(fieldRoleVisual(measure).label).toBe('度量');
    expect(fieldRoleVisual(unset).label).toBe('待确认');
  });
});

describe('紧凑场景的文字色', () => {
  it('每个角色都有对应的文字色，画布与编辑器同一套编码', () => {
    for (const field of [primary, foreign, dimension, time, measure, unset]) {
      expect(FIELD_ROLE_TEXT_CLASS[fieldRoleVisual(field).tone]).toBeTruthy();
    }
  });

  it('标识族与切分族在画布上也不同色', () => {
    expect(FIELD_ROLE_TEXT_CLASS[fieldRoleVisual(primary).tone]).not.toBe(
      FIELD_ROLE_TEXT_CLASS[fieldRoleVisual(dimension).tone],
    );
  });
});
