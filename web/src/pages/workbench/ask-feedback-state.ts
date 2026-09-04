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
 * 一条「系统没直接听懂的说法」。
 *
 * 不是「没接住」——四种收场里有两种其实答上来了：反问一次之后用户自己选了
 * （clarified），或者模型挑了个成员硬答（inferred）。共同点在过程不在结果：
 * 用户的说法没被词表直接覆盖，系统只能靠问、靠猜，或者放弃。猜对了也要补，
 * 因为下一次可能猜错。
 *
 * 页面按**能不能直接动手**排序：带正解的排前面（用户已经把答案告诉系统了，
 * 照着补别名即可），拒答排后面（还得先诊断原因）。
 */
export interface FeedbackRow {
  /**
   * 列表渲染用的稳定标识，与聚合口径同源。
   *
   * 组件那边曾经自己拼一个 `kind-question-code-resolution`——漏了 phrase，于是
   * 同一句问话下按不同说法聚合出的两行拿到完全相同的 React key，React 复用错
   * DOM 节点：切到归档时上一屏待办的行留在原地，× 和「恢复」并存。key 由聚合
   * 方生成，两者就不可能再漂移。
   */
  key: string;
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
  /**
   * 用户说了、系统没听懂的那个词。有它就按它聚合——同一个「业绩」被猜 20 次是
   * **一件事**，摊成 20 行会让真正要补的东西淹在里面。
   */
  phrase: string;
  /** 这些记录的行 id：一次把这一组全标成已处理，不用逐条点。 */
  ids: number[];
  /** 例句，最多 3 条。聚合之后要能看出这个说法是在什么语境下出现的。 */
  questions: string[];
}

/**
 * 这条记录里"用户说了、系统没听懂"的那个词。
 *
 * 模型报的配对优先——它带成员，预填时两个框都能填上；没有就退回 span 补集（模型会
 * 漏报，实测同一个「业绩」一轮报了一轮没报）。两个都没有就返回空串，那条按问句聚合。
 */
export function feedbackPhrase(item: AnalyticsQueryFailure): string {
  const pair = (item.inferred_terms ?? [])[0];
  if (pair && pair[0]) return pair[0].trim();
  return ((item.unmatched_phrases ?? [])[0] ?? '').trim();
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
  // 这两种收场，收场标签加落点已经把话说完了：「模型自己猜的 → 销售金额」。
  // 再写一句"没有匹配到这个说法，模型自己选了「销售金额」"是把同一件事说第二遍，
  // 还把成员名重复一次——那一整行文字挤掉了说法和落点的位置，长一点的条目直接
  // 折成三行。留空，由界面用标签和落点表达。
  if (kind === 'clarified' || kind === 'inferred') {
    return { what: '', fixableByAlias: true };
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
    const phrase = feedbackPhrase(item);
    // 有说法就按说法聚合，没有才退回按问句——一个说法是一件事，同一句话问两次也是。
    const key = phrase
      ? `${kind} phrase:${phrase} ${resolution}`
      : `${kind} q:${question} ${item.code} ${resolution}`;
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
      if (item.id !== undefined) existing.ids.push(item.id);
      if (existing.questions.length < 3 && !existing.questions.includes(question)) {
        existing.questions.push(question);
      }
      return;
    }
    const { what, fixableByAlias } = describe(item);
    groups.set(key, {
      key,
      kind,
      question,
      code: item.code,
      what,
      resolution,
      fixableByAlias,
      count: 1,
      phrase,
      ids: item.id === undefined ? [] : [item.id],
      questions: [question],
    });
  });
  return [...groups.values()].sort(
    (a, b) =>
      KIND_ORDER[a.kind] - KIND_ORDER[b.kind] ||
      b.count - a.count ||
      a.question.localeCompare(b.question, 'zh-CN'),
  );
}

/** 词典里能承接这条证据的那个成员。 */
export interface FeedbackFixTarget {
  kind: 'metric' | 'dimension' | 'dimensionValue';
  id: string;
  name: string;
  /** 预填进术语表单的说法。没有就留空，让人自己写——不猜。 */
  phrase: string;
}

/** 判定只读当前草稿目录里的这三类成员，不读问题文本。 */
export interface FeedbackCatalogIndex {
  metrics: ReadonlyArray<{ id: string; name: string }>;
  dimensions: ReadonlyArray<{ id: string; name: string }>;
  dimensionValues: ReadonlyArray<{ id: string; display_name: string }>;
}

const sameName = (a: string, b: string) => a.trim() === b.trim();

/**
 * 这条证据能不能直接落到某个受治理成员上。
 *
 * 只认 `resolution`——它是**这一轮真的定下来的正解**（用户在澄清卡上选中的成员名，
 * 或未发布取值的近似建议），不是从问句里猜出来的。名字必须在当前草稿目录里
 * **恰好命中一个**成员；0 个或多个一律返回 null，让这条留在「要先诊断」里。
 *
 * 说法现在也能预填：记录里存了 `inferred_terms`（模型报的「这个说法→那个成员」，
 * 已过字面子串校验）和 `unmatched_phrases`（span 补集兜底）。此前只能预填绑定、
 * 说法留空，是因为那时确实没存。仍然不猜：两个来源都没有就留空让人写，拿整句问题
 * 当术语名是造假。
 */
export function feedbackFixTarget(
  row: Pick<FeedbackRow, 'kind' | 'resolution'> & { phrase?: string },
  catalog: FeedbackCatalogIndex,
): FeedbackFixTarget | null {
  const resolution = (row.resolution || '').trim();
  if (!resolution) return null;
  const phrase = (row.phrase || '').trim();

  if (row.kind === 'clarified' || row.kind === 'inferred') {
    const metrics = catalog.metrics.filter((item) => sameName(item.name, resolution));
    const dimensions = catalog.dimensions.filter((item) => sameName(item.name, resolution));
    if (metrics.length + dimensions.length !== 1) return null;
    const hit = metrics[0] ?? dimensions[0];
    return {
      kind: metrics.length === 1 ? 'metric' : 'dimension',
      id: hit.id,
      name: hit.name,
      phrase,
    };
  }

  if (row.kind === 'unknown_value') {
    const values = catalog.dimensionValues.filter((item) =>
      sameName(item.display_name, resolution),
    );
    if (values.length !== 1) return null;
    return {
      kind: 'dimensionValue',
      id: values[0].id,
      name: values[0].display_name,
      phrase,
    };
  }

  return null;
}

/** 页头三个数：全部由本页数据算出来，不编造「线上提问总数」这类拿不到的量。 */


/**
 * 按「接下来做什么」分组，不按内部收场类型分。
 *
 * 早先的分组是 clarified / inferred / unknown_value / refused 四类内部状态，会出
 * 一种自相矛盾：「说了不认识的取值」被排在「补别名就能解决」之外，可它每一条都挂着
 * 「补进词典」按钮。分组标签必须和按钮说同一件事——有落点的进「能直接补进词典」，
 * 没落点的进「要先诊断」。
 */



/**
 * 空态说什么，取决于当前看的是待办还是归档。
 *
 * 两边共用一句文案会直接说假话：站在归档上写「待办清空了」，而待办里正堆着
 * 一百多条——只是没人归档过而已。
 */
export function feedbackEmptyCopy(
  view: 'open' | 'archived',
): { title: string; hint: string } {
  if (view === 'archived') {
    return {
      title: '归档是空的',
      hint: '补进词典、或者在待办里点 × 收起来的说法，都会落到这里，随时可以恢复。',
    };
  }
  return {
    title: '待办清空了',
    hint: '用户问数时被反问、说了系统不认识的词，或者没答上来，都会出现在这里。',
  };
}
