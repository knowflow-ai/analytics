import { describe, expect, it } from 'vitest';
import { evaluationGateTask, evaluationRunInvalidations } from './publish-gate';

describe('发布前检查里的评测项', () => {
  it('服务端没有报告时说"尚未运行"', () => {
    expect(evaluationGateTask(null)).toEqual({
      key: 'evaluation',
      done: false,
      hint: '尚未运行评测',
    });
  });

  it('通过时放行并说明用例数', () => {
    const task = evaluationGateTask({ gate_passed: true, passed: 1, total: 1 });

    expect(task.done).toBe(true);
    expect(task.hint).toBe('1 条用例全部通过');
  });

  it('没全过时不放行，并把比分说出来', () => {
    // 只说"未通过"的话，用户不知道差几条。
    const task = evaluationGateTask({ gate_passed: false, passed: 2, total: 5 });

    expect(task.done).toBe(false);
    expect(task.hint).toBe('2/5 通过');
  });

  it('全过但门禁没过时仍不放行', () => {
    /**
     * gate_passed 是服务端的判定（还看准确率阈值、用例数下限），不是 passed===total
     * 就行。前端自己算等于把门禁抄成两份，迟早对不上。
     */
    const task = evaluationGateTask({ gate_passed: false, passed: 3, total: 3 });

    expect(task.done).toBe(false);
  });
});

describe('跑完评测要失效的缓存', () => {
  it('包含发布门禁读的那个查询', () => {
    /**
     * 真实故障：跑完评测只把结果塞进组件的 useState，没失效服务端查询。评测卡显示
     * 「1/1 通过」，发布前检查还写着「尚未运行评测」、发布按钮点不动。
     */
    const keys = evaluationRunInvalidations('prj_1');

    expect(keys).toContainEqual(['evaluation-latest', 'prj_1']);
  });

  it('前缀只到 projectId，不带 revision 或 etag', () => {
    /**
     * 发布面板的查询键里带着 etag，而评测本身会推动版本前进——写死完整键就会失效
     * 一个已经不存在的键，界面照旧不动。
     */
    for (const key of evaluationRunInvalidations('prj_1')) {
      expect(key).toHaveLength(2);
      expect(key[1]).toBe('prj_1');
    }
  });

  it('顺带失效发布摘要', () => {
    // 评测会存下语义索引快照，摘要里的版本信息跟着变。
    expect(evaluationRunInvalidations('prj_1')).toContainEqual(['summary', 'prj_1']);
  });
});
