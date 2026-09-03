import type { AnalyticsQueryFailure } from '@analytics/api/types';

/** 拒答原因 → 建模者视角的一句话，以及这条证据是否可能靠补别名解决。 */
const FAILURE_REASONS: Record<
  string,
  { reason: string; fixableByAlias: boolean }
> = {
  NO_SEMANTIC_MAPPING: {
    reason: '问题里的说法没有匹配到任何指标或维度',
    fixableByAlias: true,
  },
  AMBIGUOUS_SEMANTIC_ELEMENT: {
    reason: '同名成员多个，用户需要先选一个',
    fixableByAlias: false,
  },
  CROSS_FACT_METRICS_UNSUPPORTED: {
    reason: '涉及的指标属于不同事实根，当前版本不支持合并',
    fixableByAlias: false,
  },
  DIMENSION_NOT_REACHABLE: {
    reason: '指标的事实根到不了这个维度（关系路径缺失）',
    fixableByAlias: false,
  },
  UNSUPPORTED_ANALYTIC_OPERATION: {
    reason: '要求了尚未开放的分析能力',
    fixableByAlias: false,
  },
  S2SQL_CONTRADICTORY_FILTER: {
    // 模型表达不了某个条件时会用「永不成立」的过滤把它顶掉。多半是那个说法没被
    // 词典覆盖，或选中的分析范围里没有它需要的实体。
    reason: '问题里有个条件在选中的分析范围里表达不出来',
    fixableByAlias: true,
  },
};

export function failureReason(code: string): {
  reason: string;
  fixableByAlias: boolean;
} {
  return (
    FAILURE_REASONS[code] ?? {
      reason: '解析或执行阶段失败，需要看诊断细节',
      fixableByAlias: false,
    }
  );
}

export type FeedbackKind = 'clarified' | 'inferred' | 'unknown_value' | 'refused';

/**
 * 一条「系统没接住的说法」。
 *
 * 三种收场是同一个信号的三种样子。页面按**能不能直接动手**排序：带正解的排前面
 * （用户已经把答案告诉系统了，照着补别名即可），拒答排后面（还得先诊断原因）。
 */
export interface FeedbackRow {
  kind: FeedbackKind;
  question: string;
  code: string;
  /** 建模者视角的一句话：这一轮到底发生了什么。 */
  what: string;
  /** 这次的正解，没有就是空串。 */
  resolution: string;
  /** 补别名有希望解决：这类才值得优先处理。 */
  fixableByAlias: boolean;
  count: number;
}

const KIND_ORDER: Record<FeedbackKind, number> = {
  clarified: 0,
  inferred: 1,
  unknown_value: 2,
  refused: 3,
};

function describe(item: AnalyticsQueryFailure): {
  what: string;
  fixableByAlias: boolean;
} {
  const kind = item.kind ?? 'refused';
  if (kind === 'clarified') {
    return {
      what: `系统没听懂，反问了一次；用户说他要看的是「${item.resolution ?? ''}」`,
      fixableByAlias: true,
    };
  }
  if (kind === 'inferred') {
    return {
      what: `没有匹配到这个说法，模型自己选了「${item.resolution ?? ''}」`,
      fixableByAlias: true,
    };
  }
  if (kind === 'unknown_value') {
    return {
      what: item.resolution
        ? `${item.message}（最接近的是「${item.resolution}」）`
        : item.message,
      fixableByAlias: true,
    };
  }
  const { reason, fixableByAlias } = failureReason(item.code);
  return { what: reason, fixableByAlias };
}

/**
 * 同一个问句的同一种收场只占一行，按次数降序。
 *
 * 用户会反复试同一句话，逐条列出会让真正高频的说法淹没在重复里；次数本身就是
 * 优先级——被问 8 次的说法比只出现 1 次的更该补。
 */
export function feedbackRows(items: AnalyticsQueryFailure[]): FeedbackRow[] {
  const groups = new Map<string, FeedbackRow>();
  items.forEach((item) => {
    const question = (item.question || '').trim();
    if (!question) return;
    const kind = item.kind ?? 'refused';
    const resolution = item.resolution ?? '';
    const key = `${kind} ${question} ${item.code} ${resolution}`;
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
      return;
    }
    const { what, fixableByAlias } = describe(item);
    groups.set(key, {
      kind,
      question,
      code: item.code,
      what,
      resolution,
      fixableByAlias,
      count: 1,
    });
  });
  return [...groups.values()].sort(
    (a, b) =>
      KIND_ORDER[a.kind] - KIND_ORDER[b.kind] ||
      b.count - a.count ||
      a.question.localeCompare(b.question, 'zh-CN'),
  );
}
