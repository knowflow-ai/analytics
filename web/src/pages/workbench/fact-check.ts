import type {
  AnalyticsFactCheckEntry,
  AnalyticsFactCheckKind,
  AnalyticsQualityStatus,
} from '@analytics/api/types';
import { CARDINALITY_LABELS } from '@analytics/lib/labels';

/**
 * 建模期「用数据核对」的读法。
 *
 * 发布前的质量报告把同样的证据摆成一张全量清单，人要读完再回到编辑器逐个改；
 * 这里是同一份证据的另一种摆法：就在那个字段、那条关系旁边，一句话。
 * 所以这里只负责把数字读成人话，不发请求、不管状态。
 */

export type FactCheckTone = 'green' | 'red' | 'amber' | 'slate';

export interface FactCheckSummary {
  tone: FactCheckTone;
  /** 一行数字，例如「44224 行 · 唯一率 0.9% · 空值 0」。 */
  headline: string;
  /** 该怎么办；没有话说时为空。 */
  advice: string;
  /** 统计于什么时候；没量过时为空。 */
  measuredAt: string;
}

const TONE: Record<string, FactCheckTone> = {
  passed: 'green',
  confirmed: 'green',
  blocking: 'red',
  rejected: 'red',
  warning: 'amber',
  pending_review: 'amber',
};

export const factCheckKey = (kind: AnalyticsFactCheckKind, subjectId: string) =>
  `${kind}:${subjectId}`;

export const metricSubjectId = (datasetId: string, metricId: string) =>
  `${datasetId}::${metricId}`;

export const indexFactChecks = (entries: AnalyticsFactCheckEntry[]) =>
  new Map(entries.map((entry) => [factCheckKey(entry.kind, entry.subject_id), entry]));

const number = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

/** 唯一率这类比例，0.9% 与 0% 差着一个数量级，不能都显示成 1%。 */
export const percent = (value: number) => {
  const scaled = value * 100;
  if (scaled > 0 && scaled < 1) return `${scaled.toFixed(1)}%`;
  if (value >= 0.995 && value < 1) return `${scaled.toFixed(1)}%`;
  return `${Math.round(scaled)}%`;
};

const ago = (iso: string, now: Date) => {
  const minutes = Math.floor((now.getTime() - new Date(iso).getTime()) / 60000);
  if (!Number.isFinite(minutes) || minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
};

function grainHeadline(payload: Record<string, unknown>) {
  const identifiers = Array.isArray(payload.identifier_field_ids) ? payload.identifier_field_ids : [];
  // 没配主标识时没有数字可说，让 message 自己说话。
  if (identifiers.length === 0) return '';
  return `${number(payload.total_rows)} 行 · 唯一率 ${percent(number(payload.uniqueness_rate))} · 空值 ${number(payload.null_rows)}`;
}

export const cardinalityLabel = (value: string) =>
  CARDINALITY_LABELS[value as keyof typeof CARDINALITY_LABELS] ?? value;

function relationHeadline(payload: Record<string, unknown>) {
  const left = percent(number(payload.left_join_coverage));
  const right = percent(number(payload.right_join_coverage));
  const observed = typeof payload.observed_cardinality === 'string' ? payload.observed_cardinality : '';
  const fanout = Math.max(number(payload.left_fanout_factor), number(payload.right_fanout_factor));
  const parts = [`左 ${left} / 右 ${right} 命中`];
  if (observed) parts.push(`实测 ${cardinalityLabel(observed)}`);
  if (fanout > 1.005) parts.push(`扇出 ×${fanout.toFixed(1)}`);
  return parts.join(' · ');
}

function metricHeadline(payload: Record<string, unknown>) {
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const first = Array.isArray(rows[0]) ? (rows[0] as unknown[])[0] : undefined;
  if (first === undefined || first === null) return '没有取到值';
  return String(first);
}

/**
 * 把一条核对结果读成人话。没量过时返回 undefined —— 界面该显示「用数据核对」按钮，
 * 而不是一个空的、看起来像已经量过的位置。
 */
export function describeFactCheck(
  entry: AnalyticsFactCheckEntry | undefined,
  now: Date = new Date(),
): FactCheckSummary | undefined {
  const result = entry?.result;
  if (!entry || !result) return undefined;
  const payload = result.payload ?? {};
  const message = typeof payload.message === 'string' ? payload.message : '';
  let headline = '';
  if (result.kind === 'grain') headline = grainHeadline(payload);
  else if (result.kind === 'relation') headline = relationHeadline(payload);
  else if (result.kind === 'metric') headline = metricHeadline(payload);
  if (!headline) headline = message;
  return {
    tone: TONE[result.status as AnalyticsQualityStatus] ?? 'slate',
    headline,
    // 通过时不再复述一遍「一切正常」：数字已经说了。
    advice: result.status === 'passed' || result.status === 'confirmed' ? '' : message,
    measuredAt: `统计于 ${ago(result.computed_at, now)}${entry.expired ? ' · 可能已过时' : ''}`,
  };
}

/**
 * 实测基数与声明基数不一致时，给出可以一键采用的那个值。
 *
 * 这是整组核对里唯一「既能查又能改」的一处：关系基数写错不会报错，只会让扇出判定
 * 跟着错，而错的扇出会静默改变每一个跨表指标的数值。
 */
export function observedCardinalityFix(
  entry: AnalyticsFactCheckEntry | undefined,
): { declared: string; observed: string } | undefined {
  const payload = entry?.result?.payload;
  if (!payload || entry?.result?.kind !== 'relation') return undefined;
  const declared = typeof payload.declared_cardinality === 'string' ? payload.declared_cardinality : '';
  const observed = typeof payload.observed_cardinality === 'string' ? payload.observed_cardinality : '';
  if (!declared || !observed || declared === observed) return undefined;
  return { declared, observed };
}

/**
 * 一列的画像读成一行：「149 行 · 15 个取值 · 区间 -150 ~ 1000 · 例：-150、-120」。
 *
 * 客户现场那根只有 15 个取值、区间 -150 到 1000 的整数列被 AI 命名成「账户号」。
 * 这些数字在建模第一步就量出来了，人看一眼就知道它不是账号。
 */
export function describeColumnProfile(
  profile:
    | {
        row_count: number;
        non_null_count: number;
        distinct_count: number;
        min_value?: string | null;
        max_value?: string | null;
        sample_values: string[];
      }
    | undefined,
): string {
  if (!profile || profile.row_count === 0) return '';
  const parts = [`${profile.row_count} 行`, `${profile.distinct_count} 个取值`];
  const nulls = profile.row_count - profile.non_null_count;
  if (nulls > 0) parts.push(`空值 ${percent(nulls / profile.row_count)}`);
  if (profile.min_value != null && profile.max_value != null) {
    parts.push(`区间 ${profile.min_value} ~ ${profile.max_value}`);
  }
  const samples = profile.sample_values.slice(0, 3);
  if (samples.length > 0) parts.push(`例：${samples.join('、')}`);
  return parts.join(' · ');
}
