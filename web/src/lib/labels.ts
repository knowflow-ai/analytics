import type {
  AnalyticsField,
  AnalyticsModelingSummary,
  AnalyticsRelation,
  AnalyticsRevision,
} from '@analytics/api/types';

export const CARDINALITY_LABELS: Record<AnalyticsRelation['cardinality'], string> = {
  one_to_one: '一对一',
  one_to_many: '一对多',
  many_to_one: '多对一',
  many_to_many: '多对多',
};

export const CARDINALITY_OPTIONS = (
  Object.keys(CARDINALITY_LABELS) as AnalyticsRelation['cardinality'][]
).map((value) => ({ value, label: CARDINALITY_LABELS[value] }));

export function fieldRoleLabel(field: Pick<AnalyticsField, 'kind' | 'identifier_type'>) {
  switch (field.kind) {
    case 'identifier':
      return field.identifier_type === 'primary' ? '主标识' : '外部标识';
    case 'dimension':
      return '维度';
    case 'time':
      return '时间';
    case 'measure':
      return '度量';
    default:
      return '待确认';
  }
}

export const REVISION_STATE_LABELS: Record<AnalyticsRevision['state'], string> = {
  draft: '草稿',
  validated: '已校验',
  frozen: '已冻结',
  published: '已发布',
};

export const STAGE_LABELS: Record<AnalyticsModelingSummary['stage'], string> = {
  selecting_data: '待导入数据表',
  building_draft: '建模中',
  reviewing_decisions: '待确认建议',
  blocked: '存在阻断问题',
  verifying: '验证中',
  ready_to_publish: '可发布',
  published: '已发布',
};

/**
 * Cardinality implied by the identifier roles on both ends of a relation.
 * Foreign → primary is the textbook many-to-one; anything unexpected falls
 * back to many-to-many so the modeler must look at it.
 */
export function inferCardinality(
  relation: Pick<AnalyticsRelation, 'conditions'>,
  fields: readonly AnalyticsField[],
): AnalyticsRelation['cardinality'] {
  const byId = new Map(fields.map((field) => [field.id, field]));
  const pairs = relation.conditions.map((condition) => ({
    left: byId.get(condition.left_field_id)?.identifier_type,
    right: byId.get(condition.right_field_id)?.identifier_type,
  }));
  if (!pairs.length) return 'many_to_many';
  if (pairs.every((p) => p.left === 'foreign' && p.right === 'primary')) return 'many_to_one';
  if (pairs.every((p) => p.left === 'primary' && p.right === 'foreign')) return 'one_to_many';
  if (pairs.every((p) => p.left === 'primary' && p.right === 'primary')) return 'one_to_one';
  return 'many_to_many';
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** Human-readable text for an API failure, with the business code when present. */
export function describeError(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const record = error as { message: string; code?: string; status?: number };
    if (record.status === 503 && record.code === 'not_configured') {
      return '服务尚未配置数据源或模型，请先完成设置。';
    }
    if (record.status === 401) return '需要登录。';
    return record.code && record.code !== 'HTTP_ERROR' && record.code !== record.message
      ? `${record.message}（${record.code}）`
      : record.message;
  }
  return String(error);
}
