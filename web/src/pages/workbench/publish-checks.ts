import type {
  AnalyticsModelingDiagnostic,
  AnalyticsModelingQualityReport,
  AnalyticsQualityStatus,
} from '@analytics/api/types';
import { evaluationGateTask, type GateReport } from './publish-gate';

/**
 * 发布前的三道检查：什么时候自己跑，跑完给用户看什么。
 *
 * 原来这三道全靠手点：不点「校验」就没有结论，不点「开始核对」质量报告永远是空的，
 * 而发布按钮的门禁读的正是这两份结论——于是「发布」亮着、点下去后端 409，用户看到的
 * 是「点了没反应」。更糟的是这两件事**机器完全判得了**：结构校验只读当前草稿，数据
 * 质量只对数据库做只读检查，都不需要人做任何决定。让人点，只是把机器的活儿摊给了人。
 *
 * 所以改成：能自己跑的自己跑，只把**只有人能拍板的**推到眼前（指标样本的数字对不对）。
 * 判定住在组件外面才测得到——本仓没有 DOM 测试设施。
 */

export type CheckState = 'queued' | 'running' | 'passed' | 'attention' | 'blocked';

export interface PublishCheck {
  key: 'structure' | 'quality' | 'evaluation';
  label: string;
  /** 这一项在查什么。 */
  hint: string;
  state: CheckState;
  /** 结果一句话。 */
  status: string;
}

export interface QualityReportView {
  revision_etag: number;
  ready: boolean;
  blocking_count: number;
  metric_previews: ReadonlyArray<{ id: string; metric_id: string; status: AnalyticsQualityStatus }>;
}

/**
 * 这份质量报告还配得上当前草稿吗。
 *
 * 服务端按 `revision_etag` 判定（见 `modeling_quality_report_is_stale`）：草稿被改过
 * 就作废。前端必须用同一条判据，否则界面会拿一份过期报告点亮发布按钮，而服务端拒绝。
 * 注意「校验」本身也会推进 etag——所以顺序只能是先校验、再核对数据质量，反过来跑完
 * 就立刻过期。
 */
export function qualityReportIsFresh(
  report: Pick<QualityReportView, 'revision_etag'> | null,
  revisionEtag: number,
): boolean {
  return report !== null && report.revision_etag === revisionEtag;
}

export interface AutoRunInput {
  revisionState: 'draft' | 'validated' | 'frozen' | 'published';
  readOnly: boolean;
  /** 结构诊断已经拉回来了吗。 */
  diagnosticsLoaded: boolean;
  /** 结构诊断说没有阻断问题。 */
  diagnosticsReady: boolean;
  /** 这个 etag 上已经自动跑过一次了。 */
  alreadyTried: boolean;
}

/**
 * 结构校验自己跑的条件。
 *
 * 只在**没有阻断问题**时跑：校验失败会抛异常，进页面就弹一个红色 toast 等于用报错
 * 代替了本来该好好显示的诊断列表。有阻断时诊断列表已经把该修的都列出来了，不需要
 * 再撞一次墙。已经是 validated 时校验是幂等的（服务端原样返回），但没必要多发一次。
 */
export function shouldAutoValidate(input: AutoRunInput): boolean {
  return (
    !input.readOnly &&
    !input.alreadyTried &&
    input.revisionState === 'draft' &&
    input.diagnosticsLoaded &&
    input.diagnosticsReady
  );
}

export interface AutoQualityInput {
  revisionState: 'draft' | 'validated' | 'frozen' | 'published';
  readOnly: boolean;
  /** 服务端最新那份报告（还没拉回来时传 undefined）。 */
  report: Pick<QualityReportView, 'revision_etag'> | null | undefined;
  revisionEtag: number;
  alreadyTried: boolean;
}

/**
 * 数据质量自己跑的条件。
 *
 * 等到 validated 之后才跑：校验会把 etag 推进 1，在那之前跑出来的报告立刻就过期，
 * 白白对数据库做一遍只读扫描。已有一份还新鲜的报告就不重跑。
 */
export function shouldAutoRunQuality(input: AutoQualityInput): boolean {
  if (input.readOnly || input.alreadyTried) return false;
  if (input.revisionState !== 'validated') return false;
  if (input.report === undefined) return false;
  return !qualityReportIsFresh(input.report, input.revisionEtag);
}

export interface ChecksInput {
  revisionState: 'draft' | 'validated' | 'frozen' | 'published';
  diagnosticsLoaded: boolean;
  blockingCount: number;
  structureRunning: boolean;
  /** 自动校验失败时的原因；显示在结构校验那一格里，不弹 toast。 */
  structureError: string | null;
  qualityRunning: boolean;
  report: QualityReportView | null | undefined;
  revisionEtag: number;
  evaluation: GateReport | null;
}

const PENDING_PREVIEW = (report: QualityReportView) =>
  report.metric_previews.filter((item) => item.status === 'pending_review');

export function publishChecks(input: ChecksInput): PublishCheck[] {
  // 已发布/已冻结的版本是**过了同一道门禁才发出去的**,不该再显示成「读库中」——
  // 那一版的质量报告按当时的 etag 存着,拿现在的 etag 去比一定不新鲜,界面就会
  // 永远转圈,连「需要你确认」都被当成还在跑而藏起来。
  if (input.revisionState === 'published' || input.revisionState === 'frozen') {
    return [
      { key: 'structure', label: '结构校验', hint: '关系基数、指标覆盖、作用域唯一路径', state: 'passed', status: '已通过' },
      { key: 'quality', label: '数据质量', hint: '读真实数据核对唯一率、基数、指标样本', state: 'passed', status: '已通过' },
      { key: 'evaluation', label: '评测集', hint: '重放已确认的问题，看答案有没有变', state: 'passed', status: '已通过' },
    ];
  }
  const validated = input.revisionState !== 'draft';
  const fresh = qualityReportIsFresh(input.report ?? null, input.revisionEtag)
    ? (input.report as QualityReportView)
    : null;

  const structure: PublishCheck = {
    key: 'structure',
    label: '结构校验',
    hint: '关系基数、指标覆盖、作用域唯一路径',
    state: 'queued',
    status: '排队',
  };
  if (input.structureRunning) {
    structure.state = 'running';
    structure.status = '检查中';
  } else if (!input.diagnosticsLoaded) {
    structure.state = 'running';
    structure.status = '检查中';
  } else if (input.blockingCount > 0) {
    structure.state = 'blocked';
    structure.status = `${input.blockingCount} 个阻断问题`;
  } else if (input.structureError) {
    structure.state = 'blocked';
    structure.status = '未通过';
  } else if (validated) {
    structure.state = 'passed';
    structure.status = '无阻断';
  } else {
    structure.state = 'running';
    structure.status = '检查中';
  }

  const quality: PublishCheck = {
    key: 'quality',
    label: '数据质量',
    hint: '读真实数据核对唯一率、基数、指标样本',
    state: 'queued',
    status: '排队',
  };
  if (input.qualityRunning) {
    quality.state = 'running';
    quality.status = '读库中';
  } else if (structure.state === 'blocked') {
    quality.state = 'queued';
    quality.status = '等结构校验';
  } else if (!fresh) {
    quality.state = 'running';
    quality.status = '读库中';
  } else if (fresh.blocking_count > 0) {
    quality.state = 'blocked';
    quality.status = `${fresh.blocking_count} 个阻断问题`;
  } else if (PENDING_PREVIEW(fresh).length > 0) {
    quality.state = 'attention';
    quality.status = `${PENDING_PREVIEW(fresh).length} 项待你确认`;
  } else {
    quality.state = 'passed';
    quality.status = '通过';
  }

  const gate = evaluationGateTask(input.evaluation);
  const evaluation: PublishCheck = {
    key: 'evaluation',
    label: '评测集',
    // 重放会把每条用例都跑一遍完整问数链路（含模型调用），所以它不自动跑：
    // 进一次页面就花掉一次模型预算，不是用户会想要的默认行为。
    hint: '重放已确认的问题，看答案有没有变',
    state: gate.done ? 'passed' : 'attention',
    status: gate.hint,
  };

  return [structure, quality, evaluation];
}

export interface ReviewItem {
  id: string;
  tone: 'blocking' | 'pending';
  badge: string;
  title: string;
  detail: string;
  hint: string;
  /** 指标样本才有：能当场判断对错的那一类。 */
  previewId: string | null;
  /** 已经标过「不对」：只提供改回来的入口，不再重复问一次。 */
  rejected: boolean;
}

const QUALITY_BLOCKING = new Set<AnalyticsQualityStatus>(['blocking', 'rejected']);

export interface QueueInput {
  diagnostics: ReadonlyArray<AnalyticsModelingDiagnostic>;
  report: AnalyticsModelingQualityReport | null;
  /** 语义 id → 业务名。报告按 id 说话，用户读不懂。 */
  names: ReadonlyMap<string, string>;
}

const label = (names: ReadonlyMap<string, string>, id: string) =>
  names.get(id) ?? id.split(':').pop() ?? id;

/**
 * 挡在「能发布」前面、需要人做点什么的东西。
 *
 * 只收两类：机器判定为阻断的（要回去改建模），和机器判不了、只有人能拍板的
 * （指标样本的数字对不对）。其余几十项自动通过的检查折叠起来，默认不看。
 */
export function reviewQueue({ diagnostics, report, names }: QueueInput): ReviewItem[] {
  const items: ReviewItem[] = [];

  diagnostics
    .filter((item) => item.blocking)
    .forEach((item, index) => {
      items.push({
        id: `diagnostic:${item.diagnostic_code}:${index}`,
        tone: 'blocking',
        badge: '阻断',
        title: item.title,
        detail: item.message,
        hint: item.recommended_action ?? '',
        previewId: null,
        rejected: false,
      });
    });

  if (!report) return items;

  const pushBlocking = (id: string, title: string, message: string) => {
    items.push({
      id,
      tone: 'blocking',
      badge: '阻断',
      title,
      detail: message,
      hint: '数据和声明对不上，回「实体与关系」改完会自动重新核对。',
      previewId: null,
      rejected: false,
    });
  };

  report.model_grains
    .filter((row) => row.identifier_field_ids.length > 0 && QUALITY_BLOCKING.has(row.status))
    .forEach((row) => pushBlocking(`grain:${row.model_id}`, label(names, row.model_id), row.message));

  report.relations
    .filter((row) => QUALITY_BLOCKING.has(row.status))
    .forEach((row) =>
      pushBlocking(`relation:${row.relation_id}`, label(names, row.relation_id), row.message),
    );

  report.reachability
    .filter((row) => QUALITY_BLOCKING.has(row.status))
    .forEach((row) =>
      pushBlocking(
        `reach:${row.metric_id}:${row.dimension_id}`,
        `${label(names, row.metric_id)} × ${label(names, row.dimension_id)}`,
        row.message,
      ),
    );

  report.metric_previews.forEach((row) => {
    if (row.status === 'pending_review') {
      items.push({
        id: `preview:${row.id}`,
        tone: 'pending',
        badge: '待核对',
        title: label(names, row.metric_id),
        detail: (row.rows?.[0] ?? []).map((value) => String(value)).join(' , ') || '—',
        hint: '这是用真实数据算出来的样本值。和你心里的口径对得上吗？',
        previewId: row.id,
        rejected: false,
      });
      return;
    }
    if (row.status === 'rejected') {
      items.push({
        id: `preview:${row.id}`,
        tone: 'blocking',
        // 「不对」不是把这条从队列里划掉:标成不对之后它是**阻断**,服务端也按
        // 阻断算(REJECTED 计入 blocking_count)。早先的稿子里两个按钮做同一件事,
        // 点「不对」照样能发布——那等于把用户的否决当成了同意。
        badge: '你标了不对',
        title: label(names, row.metric_id),
        detail: (row.rows?.[0] ?? []).map((value) => String(value)).join(' , ') || '—',
        hint: '指标定义要改：回「语义建模」调聚合或过滤，改完会自动重新核对。',
        previewId: row.id,
        rejected: true,
      });
      return;
    }
    if (QUALITY_BLOCKING.has(row.status)) {
      items.push({
        id: `preview:${row.id}`,
        tone: 'blocking',
        badge: '阻断',
        title: label(names, row.metric_id),
        detail: row.message,
        hint: '样本取不出来，多半是指标定义或路径有问题。',
        previewId: null,
        rejected: false,
      });
    }
  });

  return items;
}

/** 能不能发布：三道检查都过，且队列空。 */
export function publishReady(checks: ReadonlyArray<PublishCheck>, queue: ReadonlyArray<ReviewItem>): boolean {
  return queue.length === 0 && checks.every((check) => check.state === 'passed');
}

export interface Headline {
  title: string;
  sub: string;
  tone: 'running' | 'blocked' | 'attention' | 'ok';
}

export function publishHeadline({
  checks,
  queue,
  revisionState,
  autoPassed,
}: {
  checks: ReadonlyArray<PublishCheck>;
  queue: ReadonlyArray<ReviewItem>;
  revisionState: 'draft' | 'validated' | 'frozen' | 'published';
  autoPassed: number;
}): Headline {
  if (revisionState === 'published') {
    return { title: '本版本已发布', sub: '线上问数正在使用这一版。', tone: 'ok' };
  }
  if (checks.some((check) => check.state === 'running')) {
    return {
      title: '正在检查这版能不能发布…',
      sub: '进入这一步就自动开始，不用点任何按钮；离开页面也会跑完。',
      tone: 'running',
    };
  }
  const blockers = queue.filter((item) => item.tone === 'blocking');
  if (blockers.length > 0) {
    return {
      title: `${blockers.length} 个阻断问题，得先回去改建模`,
      sub: '改完自动重新检查，不用回来点按钮。',
      tone: 'blocked',
    };
  }
  const pending = queue.length;
  if (pending > 0) {
    return {
      title: `还差 ${pending} 件事：只有你能判断这些数字对不对`,
      sub: `其余 ${autoPassed} 项检查已自动跑完并通过。`,
      tone: 'attention',
    };
  }
  const evaluation = checks.find((check) => check.key === 'evaluation');
  if (evaluation && evaluation.state !== 'passed') {
    return {
      title: '还差评测集：重放一遍已确认的问题',
      sub: '评测会把每条用例跑一遍完整问数链路，所以不自动跑。',
      tone: 'attention',
    };
  }
  return {
    title: '全部通过，可以发布',
    sub: '发布会冻结完整目录并建立语义索引。',
    tone: 'ok',
  };
}

/**
 * 折叠起来的「自动通过」计数：跑过的检查项减去还挂在队列里的。
 *
 * 刻意不计非阻断诊断（提醒）：提醒是「跑完了但有话说」，不是通过。把它算进去,
 * 折叠条会写着「45 项自动通过」而展开后头三条是黄色提醒——数字和内容对不上。
 */
export function autoPassedCount(
  report: AnalyticsModelingQualityReport | null,
  queue: ReadonlyArray<ReviewItem>,
): number {
  if (!report) return 0;
  const checked =
    report.model_grains.filter((row) => row.identifier_field_ids.length > 0).length +
    report.relations.length +
    report.metric_previews.length +
    report.reachability.length;
  return Math.max(0, checked - queue.length);
}
