import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listQueryFailures } from "@analytics/api/analytics";
import { Badge, Empty, Spinner } from "@analytics/components/ui";
import { groupFailures } from "./ask-feedback-state";
import type { WorkbenchContext } from "./index";

type Context = Pick<WorkbenchContext, "projectId">;

/**
 * 问数反馈：线上真实提问回流到建模。
 *
 * 被拒答的问题是"用户说了什么、系统听不懂什么"的直接证据；这里把它们按
 * 可操作性排好，标出哪些靠补别名或术语就能解决。采纳仍走目录编辑与发布流程，
 * 一次线上提问不是长期业务事实，不允许直接改 Active Release。
 */
export function AskFeedbackPanel({ projectId }: Context) {
  const failures = useQuery({
    queryKey: ["analytics-query-failures", projectId],
    queryFn: () => listQueryFailures(projectId, 200),
  });

  const failureRows = useMemo(
    () => groupFailures(failures.data?.items ?? []),
    [failures.data],
  );

  return (
    <div className="space-y-4">
      <section>
        <header className="mb-2 flex items-baseline justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-800">
            没答上来的问题
          </h3>
          <span className="text-xs text-slate-400">
            按被拒次数排序；标为「补别名或术语」的最值得先处理
          </span>
        </header>
        {failures.isPending ? (
          <Spinner label="加载中" />
        ) : failureRows.length ? (
          <div className="overflow-auto rounded-lg border border-slate-200">
            <table className="w-full min-w-[680px] text-left text-xs">
              <thead className="bg-slate-50 text-[11px] text-slate-400">
                <tr>
                  <th className="px-3 py-2 font-medium">问过但没答上的问题</th>
                  <th className="px-3 py-2 font-medium">次数</th>
                  <th className="px-3 py-2 font-medium">可能的解法</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {failureRows.map((row) => (
                  <tr key={`${row.question}-${row.code}`}>
                    <td className="px-3 py-2">
                      <div className="text-slate-700">{row.question}</div>
                      <div className="text-[11px] text-slate-400">
                        {row.reason}
                      </div>
                    </td>
                    <td className="px-3 py-2 tabular-nums text-slate-600">
                      {row.count}
                    </td>
                    <td className="px-3 py-2">
                      {row.fixableByAlias ? (
                        <Badge tone="amber">补别名或术语</Badge>
                      ) : (
                        <span className="text-slate-400">需按原因处理</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="还没有被拒答的问题" />
        )}
      </section>
    </div>
  );
}
