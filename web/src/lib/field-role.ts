import type { AnalyticsField } from '@analytics/api/types';
import type { BadgeTone, BadgeVariant } from '@analytics/components/ui';

import { fieldRoleLabel } from './labels';

/**
 * 字段角色的视觉编码。
 *
 * 原则：同族同色相、族内用强度（实心/描边）或图标区分，跨族才换色相。
 * 这样一眼能读出「这张表 1 主键 1 外键 3 切分 2 度量」的结构，而不是
 * 一排看不出差别的灰底徽章。blue 保留给操作（按钮、链接），不做状态色。
 */
export interface FieldRoleVisual {
  label: string;
  tone: BadgeTone;
  variant: BadgeVariant;
  /** 时间是切分族的特例：不再开一个色相，用图标区分。 */
  icon: 'clock' | null;
}

type RoleInput = Pick<AnalyticsField, 'kind' | 'identifier_type'>;

export function fieldRoleVisual(field: RoleInput): FieldRoleVisual {
  const label = fieldRoleLabel(field);
  switch (field.kind) {
    case 'identifier':
      // 标识族：主标识每表唯一，最强；外部标识同色相弱一档。
      return field.identifier_type === 'primary'
        ? { label, tone: 'violet', variant: 'solid', icon: null }
        : { label, tone: 'violet', variant: 'outline', icon: null };
    case 'dimension':
      return { label, tone: 'sky', variant: 'solid', icon: null };
    case 'time':
      return { label, tone: 'sky', variant: 'outline', icon: 'clock' };
    case 'measure':
      return { label, tone: 'green', variant: 'solid', icon: null };
    default:
      // 待确认是需要用户处理的缺口，用提醒色。
      return { label, tone: 'amber', variant: 'solid', icon: null };
  }
}

/**
 * 紧凑场景（画布节点）没有徽章底色的空间，用同一套色相的文字色表达角色，
 * 保证画布和实体编辑器读到的是同一套编码。
 */
export const FIELD_ROLE_TEXT_CLASS: Record<BadgeTone, string> = {
  slate: 'text-slate-400',
  blue: 'text-blue-600',
  green: 'text-emerald-600',
  amber: 'text-amber-600',
  red: 'text-red-600',
  violet: 'text-violet-600',
  sky: 'text-sky-600',
};
