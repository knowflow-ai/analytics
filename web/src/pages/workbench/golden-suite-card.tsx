import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ClipboardCheck, X } from 'lucide-react';
import { useState } from 'react';
import {
  deleteGoldenSuite,
  evaluateSuite,
  listGoldenSuites,
  saveGoldenSuite,
  versionOf,
} from '@analytics/api/analytics';
import type { AnalyticsEvaluationReport, AnalyticsRevision } from '@analytics/api/types';
import { Badge, Button, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

/**
 * 评测集:发布前的量化关。
 *
 * 用例不靠表单手填——期望字段有二十多个,手填是灾难。唯一入口是试跑:
 * 问了、答对了、点「存为评测用例」,期望取本次真实解析结果。这里只负责
 * 列出、删除和跑分。
 */

export function GoldenSuiteCard({
  projectId,
  revision,
  readOnly,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  readOnly: boolean;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const suites = useQuery({
    queryKey: ['golden', projectId, revision.id],
    queryFn: () => listGoldenSuites(projectId, revision.id),
  });
  const [report, setReport] = useState<AnalyticsEvaluationReport | null>(null);
  const record = suites.data?.items[0] ?? null;

  const run = useMutation({
    mutationFn: () => evaluateSuite(projectId, revision.id, record!.suite),
    onSuccess: setReport,
    onError: (error) => toast.error(describeError(error)),
  });
  const toggleMemory = useMutation({
    // 少样本 = 已人工确认的用例。切换只改 memory 字段,评测重放不受影响;
    // 服务端召回侧还有资格门(COMPLETED/非评测标签/版本一致),这里只是开关。
    mutationFn: async (caseId: string) => {
      const cases = record!.suite.cases.map((item) => {
        if (item.id !== caseId) return item;
        const enabled = item.memory_status === 'ENABLED';
        return {
          ...item,
          memory_status: enabled ? ('DISABLED' as const) : ('ENABLED' as const),
          memory_review_result: enabled ? undefined : ('POSITIVE' as const),
        };
      });
      await saveGoldenSuite(projectId, revision.id, versionOf(revision), {
        ...record!.suite,
        cases,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['golden', projectId, revision.id] });
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const removeCase = useMutation({
    mutationFn: async (caseId: string): Promise<void> => {
      const remaining = record!.suite.cases.filter((item) => item.id !== caseId);
      if (remaining.length === 0) {
        // 契约要求套件至少一条用例;删到空就删整个套件。
        await deleteGoldenSuite(projectId, revision.id, versionOf(revision), record!.suite.id);
        return;
      }
      await saveGoldenSuite(projectId, revision.id, versionOf(revision), {
        ...record!.suite,
        cases: remaining,
      });
    },
    onSuccess: () => {
      setReport(null);
      queryClient.invalidateQueries({ queryKey: ['golden', projectId, revision.id] });
    },
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <div className="mt-6 rounded-lg border border-slate-200 p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <ClipboardCheck className="h-4 w-4 text-slate-400" /> 评测集
            {record && <span className="text-xs font-normal text-slate-400">{record.suite.cases.length} 条用例</span>}
          </h3>
          <p className="mt-0.5 text-xs text-slate-500">
            改完模型跑一遍,确认以前答对的问题现在还答对。
          </p>
        </div>
        {record && (
          <Button variant="default" loading={run.isPending} onClick={() => run.mutate()}>
            跑评测
          </Button>
        )}
      </div>

      {suites.isPending && <Spinner />}
      {!suites.isPending && !record && (
        <div className="mt-3 rounded-md border border-dashed border-slate-200 px-3 py-2 text-[11px] text-slate-400">
          还没有用例。在下方「发布前试问」里问一个问题,答对后点「存为评测用例」。
        </div>
      )}

      {report && (
        <div className="mt-3 flex items-center gap-2 text-xs">
          <Badge tone={report.gate_passed ? 'green' : 'red'}>
            {report.passed}/{report.total} 通过
          </Badge>
          <span className="text-slate-500">准确率 {(report.accuracy * 100).toFixed(0)}%</span>
        </div>
      )}

      {record && (
        <ul className="mt-3 flex flex-col gap-1.5 text-xs">
          {record.suite.cases.map((item) => {
            const result = report?.results.find((r) => r.case_id === item.id);
            return (
              <li
                key={item.id}
                className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 ${
                  result && !result.passed ? 'border-red-200 bg-red-50/50' : 'border-slate-200'
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="text-slate-800">「{item.question}」</div>
                  {result && !result.passed && (
                    <div className="mt-0.5 whitespace-pre-wrap break-words text-red-700">{result.message}</div>
                  )}
                </div>
                {result && <Badge tone={result.passed ? 'green' : 'red'}>{result.passed ? '通过' : '未过'}</Badge>}
                {!readOnly && (
                  <button
                    type="button"
                    title={
                      item.memory_status === 'ENABLED'
                        ? '已确认,会作为少样本示例供相似问题参考;点击停用'
                        : '未用于少样本;点击确认无误并启用'
                    }
                    disabled={toggleMemory.isPending}
                    onClick={() => toggleMemory.mutate(item.id)}
                  >
                    <Badge tone={item.memory_status === 'ENABLED' ? 'sky' : 'slate'} variant="outline">
                      少样本{item.memory_status === 'ENABLED' ? ' ✓' : ''}
                    </Badge>
                  </button>
                )}
                {!readOnly && (
                  <button
                    type="button"
                    className="text-slate-300 hover:text-red-600"
                    onClick={() => removeCase.mutate(item.id)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
