import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listQueryFailures } from "@analytics/api/analytics";
import { Badge, Empty, Spinner } from "@analytics/components/ui";
import { feedbackRows, type FeedbackKind } from "./ask-feedback-state";
import type { WorkbenchContext } from "./index";

type Context = Pick<WorkbenchContext, "projectId">;

/** 三种收场的说人话标签。用户读到的必须是"发生了什么"，不是内部状态名。 */
const KIND_LABEL: Record<FeedbackKind, string> = {
  clarified: "用户替系统补了答案",
  inferred: "模型自己猜的",
  unknown_value: "说了系统不认识的词",
  refused: "没答上来",
};

const KIND_TONE: Record<FeedbackKind, "green" | "violet" | "amber" | "blue"> = {
  clarified: "green",
  inferred: "violet",
  unknown_value: "amber",
  refused: "blue",
};

/**
 * 问数反馈：线上真实提问回流到建模。
 *
 * 这一页只回答一个问题：**用户说了什么，系统没接住？**
 *
 * 三种收场是同一个信号的三种样子，所以放在一张表里，按能不能直接动手排序：
 * 用户澄清后答出来的自带正解（照着补别名就行）；模型自己猜的正解不可靠，但同一说法
 * 被猜过很多次本身就是该补词典的信号；说了不认识的词的往往是拼写或说法没覆盖；
 * 没答上来的还得先诊断原因。
 *
 * 治理边界：这里只呈现证据，不改任何东西。补别名要到对应的指标/维度里去做，
 * 仍然走草稿版本与发布流程——一次线上提问不是长期业务口径。
 */
export function AskFeedbackPanel({ projectId }: Context) {
  const failures = useQuery({
    queryKey: ["analytics-query-failures", projectId],
    queryFn: () => listQueryFailures(projectId, 200),
  });

  const rows = useMemo(
    () => feedbackRows(failures.data?.items ?? []),
    [failures.data],
  );

  return (
    <div className="space-y-4">
      <section>
        <header className="mb-2 flex items-baseline justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-800">
            用户说了什么，系统没接住
          </h3>
          <span className="text-xs text-slate-400">
            带答案的排在前面；标为「补别名或术语」的最值得先处理
          </span>
        </header>
        {failures.isPending ? (
          <Spinner label="加载中" />
        ) : rows.length ? (
          <div className="overflow-auto rounded-lg border border-slate-200">
            <table className="w-full min-w-[760px] text-left text-xs">
              <thead className="bg-slate-50 text-[11px] text-slate-400">
                <tr>
                  <th className="px-3 py-2 font-medium">用户问的</th>
                  <th className="px-3 py-2 font-medium">发生了什么</th>
                  <th className="px-3 py-2 font-medium">次数</th>
                  <th className="px-3 py-2 font-medium">可能的解法</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.map((row) => (
                  <tr key={`${row.kind}-${row.question}-${row.code}-${row.resolution}`}>
                    <td className="px-3 py-2">
                      <div className="text-slate-700">{row.question}</div>
                      <div className="mt-1">
                        <Badge tone={KIND_TONE[row.kind]}>
                          {KIND_LABEL[row.kind]}
                        </Badge>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-slate-500">{row.what}</td>
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
          <Empty
            title="还没有这类记录"
            hint="用户问数时被反问、说了系统不认识的词，或者没答上来，都会出现在这里。"
          />
        )}
      </section>
    </div>
  );
}
