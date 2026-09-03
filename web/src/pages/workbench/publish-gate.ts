/**
 * 发布前检查的读法。
 *
 * 抽出来是因为踩过一次：跑完评测的结果只进了组件自己的 useState，服务端那份没被
 * 重新拉取，于是评测卡显示「1/1 通过」、右边发布前检查还写着「尚未运行评测」、
 * 发布按钮点不动——**两个面板对着同一次运行给出相反的说法**，用户只能靠刷新页面
 * 撞出来。
 *
 * 判定住在组件外面才测得到（本仓没有 DOM 测试设施）。
 */

export interface GateReport {
  gate_passed?: boolean;
  passed?: number;
  total?: number;
}

export interface GateTask {
  key: string;
  done: boolean;
  hint: string;
}

/** 评测那一项的状态。``null`` = 服务端还没有这次修订的报告。 */
export function evaluationGateTask(report: GateReport | null): GateTask {
  if (report === null) {
    return { key: 'evaluation', done: false, hint: '尚未运行评测' };
  }
  if (report.gate_passed) {
    return { key: 'evaluation', done: true, hint: `${report.total} 条用例全部通过` };
  }
  return {
    key: 'evaluation',
    done: false,
    hint: `${report.passed}/${report.total} 通过`,
  };
}

/**
 * 跑完评测要让哪些缓存失效。
 *
 * 前缀写到 projectId 为止，不带 revision/etag：发布面板的查询键里带着 etag，
 * 而评测本身会推动版本前进——写死完整键就会失效一个已经不存在的键，界面照旧不动。
 */
export function evaluationRunInvalidations(projectId: string): string[][] {
  return [
    ['evaluation-latest', projectId],
    ['summary', projectId],
  ];
}
