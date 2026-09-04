import { describe, expect, it } from 'vitest';
import {
  autoPassedCount,
  publishChecks,
  publishHeadline,
  publishReady,
  qualityReportIsFresh,
  reviewQueue,
  shouldAutoRunQuality,
  shouldAutoValidate,
} from './publish-checks';

const report = (overrides: Record<string, unknown> = {}) =>
  ({
    id: 'qr_1',
    revision_etag: 7,
    ready: true,
    blocking_count: 0,
    status: 'completed',
    model_grains: [],
    relations: [],
    metric_previews: [],
    reachability: [],
    ...overrides,
  }) as any;

const diagnostic = (overrides: Record<string, unknown> = {}) =>
  ({
    diagnostic_code: 'RELATION_CARDINALITY_MISMATCH',
    title: '门店 → 销售单',
    message: '声明 one_to_many，实测 many_to_many',
    resource_kind: 'relation',
    affected_resource_ids: [],
    blocking: true,
    recommended_action: '改基数，或补上缺的关联条件',
    ...overrides,
  }) as any;

const checksInput = (overrides: Record<string, unknown> = {}) => ({
  revisionState: 'validated' as const,
  diagnosticsLoaded: true,
  blockingCount: 0,
  structureRunning: false,
  structureError: null,
  qualityRunning: false,
  report: report(),
  revisionEtag: 7,
  evaluation: { gate_passed: true, passed: 3, total: 3 },
  ...overrides,
});

describe('质量报告还新鲜吗', () => {
  it('etag 一致才算数', () => {
    expect(qualityReportIsFresh(report({ revision_etag: 7 }), 7)).toBe(true);
  });

  it('草稿被改过就作废', () => {
    /**
     * 服务端按 revision_etag 判定（modeling_quality_report_is_stale）。前端用别的
     * 判据就会拿一份过期报告点亮发布按钮，而服务端拒绝——用户看到「点了没反应」。
     */
    expect(qualityReportIsFresh(report({ revision_etag: 6 }), 7)).toBe(false);
  });

  it('没有报告就是不新鲜', () => {
    expect(qualityReportIsFresh(null, 7)).toBe(false);
  });
});

describe('结构校验自己跑的条件', () => {
  const input = {
    revisionState: 'draft' as const,
    readOnly: false,
    diagnosticsLoaded: true,
    diagnosticsReady: true,
    alreadyTried: false,
  };

  it('草稿 + 无阻断时自己跑，不用点按钮', () => {
    expect(shouldAutoValidate(input)).toBe(true);
  });

  it('有阻断时不跑', () => {
    /**
     * 校验失败会抛异常：进页面就弹一个红 toast，等于用报错代替了本来该好好显示的
     * 诊断列表。有阻断时列表已经把该修的都列出来了，不需要再撞一次墙。
     */
    expect(shouldAutoValidate({ ...input, diagnosticsReady: false })).toBe(false);
  });

  it('诊断还没回来时不跑', () => {
    expect(shouldAutoValidate({ ...input, diagnosticsLoaded: false })).toBe(false);
  });

  it('只读版本不跑', () => {
    expect(shouldAutoValidate({ ...input, readOnly: true })).toBe(false);
  });

  it('已经是 validated 就不用再跑', () => {
    expect(shouldAutoValidate({ ...input, revisionState: 'validated' })).toBe(false);
  });

  it('同一个 etag 上只跑一次', () => {
    expect(shouldAutoValidate({ ...input, alreadyTried: true })).toBe(false);
  });
});

describe('数据质量自己跑的条件', () => {
  const input = {
    revisionState: 'validated' as const,
    readOnly: false,
    report: null,
    revisionEtag: 7,
    alreadyTried: false,
  };

  it('校验过、还没有报告时自己跑', () => {
    expect(shouldAutoRunQuality(input)).toBe(true);
  });

  it('等校验完成再跑', () => {
    /**
     * 校验会把 etag 推进 1，在那之前跑出来的报告立刻过期——白白对数据库做一遍
     * 只读扫描，用户还要等。顺序只能是先校验、后核对。
     */
    expect(shouldAutoRunQuality({ ...input, revisionState: 'draft' })).toBe(false);
  });

  it('报告还没拉回来时不跑', () => {
    // undefined 是「还在请求」，不是「没有」。分不清就会每次进页面都多跑一遍。
    expect(shouldAutoRunQuality({ ...input, report: undefined })).toBe(false);
  });

  it('已有新鲜报告就不重跑', () => {
    expect(
      shouldAutoRunQuality({ ...input, report: { revision_etag: 7 } }),
    ).toBe(false);
  });

  it('报告过期了要重跑', () => {
    expect(
      shouldAutoRunQuality({ ...input, report: { revision_etag: 6 } }),
    ).toBe(true);
  });

  it('只读版本不跑', () => {
    expect(shouldAutoRunQuality({ ...input, readOnly: true })).toBe(false);
  });
});

describe('三道检查的显示', () => {
  it('全通过时三格都是 passed', () => {
    const checks = publishChecks(checksInput());

    expect(checks.map((check) => check.state)).toEqual(['passed', 'passed', 'passed']);
  });

  it('有阻断时数据质量排队等结构校验，而不是假装自己也在跑', () => {
    const checks = publishChecks(
      checksInput({ revisionState: 'draft', blockingCount: 2, report: null }),
    );

    expect(checks[0]).toMatchObject({ state: 'blocked', status: '2 个阻断问题' });
    expect(checks[1]).toMatchObject({ state: 'queued', status: '等结构校验' });
  });

  it('过期的报告等于没有报告', () => {
    const checks = publishChecks(checksInput({ report: report({ revision_etag: 6 }) }));

    expect(checks[1].state).toBe('running');
  });

  it('待核对的指标样本让数据质量显示需要注意，不是通过', () => {
    const checks = publishChecks(
      checksInput({
        report: report({
          metric_previews: [{ id: 'p1', metric_id: 'metric:a', status: 'pending_review' }],
        }),
      }),
    );

    expect(checks[1]).toMatchObject({ state: 'attention', status: '1 项待你确认' });
  });

  it('评测集没跑过时不是 passed——但它也不自己跑', () => {
    // 重放会把每条用例跑一遍完整问数链路（含模型调用）。进一次页面花一次预算,
    // 不该是默认行为。
    const checks = publishChecks(checksInput({ evaluation: null }));

    expect(checks[2]).toMatchObject({ state: 'attention', status: '尚未运行评测' });
  });

  it('已发布的版本三格都是已通过，不再转圈', () => {
    /**
     * 那一版的质量报告按当时的 etag 存着,拿现在的 etag 去比一定不新鲜。不特判的话
     * 界面永远显示「读库中」,连「需要你确认」都会被当成还在跑而藏起来。
     */
    const checks = publishChecks(checksInput({ revisionState: 'published', report: null, evaluation: null }));

    expect(checks.map((check) => check.state)).toEqual(['passed', 'passed', 'passed']);
  });

  it('自动校验失败时把原因显示在格子里，不弹 toast', () => {
    const checks = publishChecks(checksInput({ revisionState: 'draft', structureError: '仍有未审核的建议' }));

    expect(checks[0].state).toBe('blocked');
  });
});

describe('需要人做点什么的队列', () => {
  const names = new Map([
    ['metric:sales', '销售金额'],
    ['rel:1', '门店 → 销售单'],
  ]);

  it('阻断诊断进队列，并带上修复建议', () => {
    const queue = reviewQueue({ diagnostics: [diagnostic()], report: null, names });

    expect(queue).toHaveLength(1);
    expect(queue[0]).toMatchObject({ tone: 'blocking', title: '门店 → 销售单' });
    expect(queue[0].hint).toContain('改基数');
  });

  it('提醒类诊断不进队列', () => {
    // 队列只放「挡着发布的东西」。提醒混进来会让人以为必须处理完才能发。
    const queue = reviewQueue({
      diagnostics: [diagnostic({ blocking: false })],
      report: null,
      names,
    });

    expect(queue).toHaveLength(0);
  });

  it('待核对的指标样本进队列，能当场判断', () => {
    const queue = reviewQueue({
      diagnostics: [],
      report: report({
        metric_previews: [
          {
            id: 'p1',
            metric_id: 'metric:sales',
            status: 'pending_review',
            rows: [[128430.5]],
            message: '',
          },
        ],
      }),
      names,
    });

    expect(queue[0]).toMatchObject({
      tone: 'pending',
      title: '销售金额',
      previewId: 'p1',
      rejected: false,
    });
  });

  it('标了「不对」的样本留在队列里，而且是阻断', () => {
    /**
     * 这是设计稿里最严重的一处错：两个按钮做同一件事——点「不对」也把这条从队列里
     * 划掉，于是「数字不对」照样能发布。服务端其实是对的（REJECTED 计入
     * blocking_count），界面必须说同一件事：否决不是同意。
     */
    const queue = reviewQueue({
      diagnostics: [],
      report: report({
        metric_previews: [
          { id: 'p1', metric_id: 'metric:sales', status: 'rejected', rows: [[1]], message: '' },
        ],
      }),
      names,
    });

    expect(queue).toHaveLength(1);
    expect(queue[0]).toMatchObject({ tone: 'blocking', rejected: true, previewId: 'p1' });
  });

  it('未配置主标识的模型不进队列', () => {
    // 那是「这一项查不了」的前置条件，结构诊断已经单独报过；两处都报会让用户
    // 修好一处后发现另一处还在。
    const queue = reviewQueue({
      diagnostics: [],
      report: report({
        model_grains: [
          { model_id: 'm1', identifier_field_ids: [], status: 'blocking', message: '没有主标识' },
        ],
      }),
      names,
    });

    expect(queue).toHaveLength(0);
  });

  it('语义 id 翻成业务名', () => {
    const queue = reviewQueue({
      diagnostics: [],
      report: report({
        relations: [{ relation_id: 'rel:1', status: 'blocking', message: '实测 many_to_many' }],
      }),
      names,
    });

    expect(queue[0].title).toBe('门店 → 销售单');
  });
});

describe('能不能发布', () => {
  it('三道全过且队列空才行', () => {
    expect(publishReady(publishChecks(checksInput()), [])).toBe(true);
  });

  it('队列里还有东西就不行', () => {
    const queue = reviewQueue({ diagnostics: [diagnostic()], report: null, names: new Map() });

    expect(publishReady(publishChecks(checksInput()), queue)).toBe(false);
  });

  it('评测没跑过就不行', () => {
    expect(publishReady(publishChecks(checksInput({ evaluation: null })), [])).toBe(false);
  });
});

describe('总结那一句话', () => {
  const base = { revisionState: 'validated' as const, autoPassed: 23 };

  it('跑的时候说清楚是自动开始的', () => {
    const headline = publishHeadline({
      ...base,
      checks: publishChecks(checksInput({ report: null })),
      queue: [],
    });

    expect(headline.tone).toBe('running');
    expect(headline.sub).toContain('不用点任何按钮');
  });

  it('有阻断时先说阻断', () => {
    const queue = reviewQueue({ diagnostics: [diagnostic()], report: null, names: new Map() });
    const headline = publishHeadline({ ...base, checks: publishChecks(checksInput()), queue });

    expect(headline).toMatchObject({ tone: 'blocked' });
    expect(headline.title).toContain('1 个阻断问题');
  });

  it('只剩人工核对时点明「只有你能判断」', () => {
    const queue = reviewQueue({
      diagnostics: [],
      report: report({
        metric_previews: [
          { id: 'p1', metric_id: 'm', status: 'pending_review', rows: [[1]], message: '' },
        ],
      }),
      names: new Map(),
    });
    const headline = publishHeadline({ ...base, checks: publishChecks(checksInput()), queue });

    expect(headline.tone).toBe('attention');
    expect(headline.title).toContain('只有你能判断');
  });

  it('全过时说可以发布', () => {
    const headline = publishHeadline({ ...base, checks: publishChecks(checksInput()), queue: [] });

    expect(headline).toMatchObject({ tone: 'ok', title: '全部通过，可以发布' });
  });
});

describe('折叠起来的自动通过计数', () => {
  it('等于跑过的项减去还挂在队列里的', () => {
    // 早先这个数是写死的 23,于是有阻断时它照样说「23 项自动通过」,还把那条
    // 正在报错的关系算了进去。
    const current = report({
      relations: [
        { relation_id: 'r1', status: 'passed', message: '' },
        { relation_id: 'r2', status: 'blocking', message: '' },
      ],
      metric_previews: [{ id: 'p1', metric_id: 'm', status: 'confirmed', rows: [], message: '' }],
    });
    const queue = reviewQueue({ diagnostics: [], report: current, names: new Map() });

    expect(autoPassedCount(current, queue)).toBe(2);
  });

  it('不把「提醒」算成通过', () => {
    /**
     * 实机截图：折叠条写着「45 项自动通过」，展开后头三条全是黄色的作用域提醒。
     * 提醒是「跑完了但有话说」，不是通过。
     */
    const current = report({
      relations: [{ relation_id: 'r1', status: 'passed', message: '' }],
    });

    expect(autoPassedCount(current, [])).toBe(1);
  });

  it('没有报告时是 0，不编一个数出来', () => {
    expect(autoPassedCount(null, [])).toBe(0);
  });
});
