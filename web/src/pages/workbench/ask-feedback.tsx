import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, MessageSquareText } from "lucide-react";
import { useMemo, useState } from "react";
import {
  listQueryFailures,
  updateQueryFailureStatus,
} from "@analytics/api/analytics";
import type { AnalyticsFeedbackStatus } from "@analytics/api/types";
import { Badge, Button, Empty, Spinner } from "@analytics/components/ui";
import {
  FEEDBACK_FILTERS,
  feedbackFixTarget,
  feedbackRows,
  feedbackSummary,
  matchesFeedbackFilter,
  type FeedbackCatalogIndex,
  type FeedbackFilter,
  type FeedbackFixTarget,
  type FeedbackKind,
} from "./ask-feedback-state";
import { ANALYTICS_TASK_PANEL_CLASS } from "@analytics/lib/layout";
import type { WorkbenchContext } from "./index";

type Context = Pick<WorkbenchContext, "projectId" | "revision" | "readOnly">;

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

const TARGET_LABEL: Record<FeedbackFixTarget["kind"], string> = {
  metric: "指标",
  dimension: "维度",
  dimensionValue: "维度值",
};

/**
 * 问数反馈：线上真实提问回流到建模。
 *
 * 这一页只回答一个问题：**下一版该补哪些说法？**
 *
 * 早先它只是一张四列宽表：证据摆在那里，但看完不知道下一步点哪里——想补一条别名，
 * 得自己记住成员名，切到语义建模，再在业务词典里翻出来。所以每条带正解的证据都给
 * 一个落点：「补进词典」直接打开对应成员的术语/维度值编辑器。
 *
 * 治理边界没变：这里仍然不改任何东西，编辑发生在业务词典里，走草稿版本与发布流程
 * ——一次线上提问不是长期业务口径。落点也绝不猜：只有 `resolution` 在当前草稿目录里
 * 恰好命中一个成员时才给按钮（见 `feedbackFixTarget`）。
 */
const PAGE_SIZE = 50;

export function AskFeedbackPanel({
  projectId,
  revision,
  readOnly,
  onFixInDictionary,
}: Context & { onFixInDictionary: (target: FeedbackFixTarget) => void }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AnalyticsFeedbackStatus>("open");
  const [offset, setOffset] = useState(0);
  const failures = useQuery({
    queryKey: ["analytics-query-failures", projectId, status, offset],
    queryFn: () => listQueryFailures(projectId, { limit: PAGE_SIZE, offset, status }),
  });
  const [filter, setFilter] = useState<FeedbackFilter>("all");
  const total = failures.data?.total ?? 0;

  const mark = useMutation({
    mutationFn: (input: { ids: number[]; next: AnalyticsFeedbackStatus }) =>
      updateQueryFailureStatus(projectId, {
        failure_ids: input.ids,
        status: input.next,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["analytics-query-failures", projectId] });
    },
  });

  const catalog: FeedbackCatalogIndex = useMemo(
    () => ({
      metrics: revision.semantic_spec.metrics,
      dimensions: revision.semantic_spec.dimensions,
      dimensionValues: revision.semantic_catalog.dimensionValues,
    }),
    [revision.semantic_catalog.dimensionValues, revision.semantic_spec],
  );

  const rows = useMemo(
    () =>
      feedbackRows(failures.data?.items ?? []).map((row) => ({
        row,
        fix: feedbackFixTarget(row, catalog),
      })),
    [catalog, failures.data],
  );
  const summary = useMemo(
    () => feedbackSummary(rows.map((item) => item.row), catalog),
    [catalog, rows],
  );
  const counts = useMemo(
    () =>
      Object.fromEntries(
        FEEDBACK_FILTERS.map((item) => [
          item.key,
          rows.filter((entry) => matchesFeedbackFilter(item.key, entry.fix)).length,
        ]),
      ) as Record<FeedbackFilter, number>,
    [rows],
  );
  const visible = rows.filter((entry) => matchesFeedbackFilter(filter, entry.fix));

  if (failures.isPending) return <Spinner label="加载中" />;

  if (rows.length === 0) {
    return (
      <div className={`px-6 py-5 ${ANALYTICS_TASK_PANEL_CLASS}`}>
        <Header />
        <Empty
          title="还没有没接住的说法"
          hint="用户问数时被反问、说了系统不认识的词，或者没答上来，都会出现在这里。"
        />
      </div>
    );
  }

  return (
    <div className={`grid min-h-[520px] grid-cols-[minmax(0,1fr)_300px] ${ANALYTICS_TASK_PANEL_CLASS}`}>
      <section className="min-w-0 px-6 py-5">
        <Header />

        <div className="mt-4 grid grid-cols-3 gap-3">
          <Stat label="没接住的说法" value={summary.sayings} unit="种" />
          <Stat
            label="累计被问"
            value={summary.occurrences}
            unit="次"
            hint="次数就是优先级"
          />
          <Stat
            label="能直接补进词典"
            value={summary.fixable}
            unit="种"
            tone={summary.fixable > 0 ? "amber" : "slate"}
          />
        </div>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          {(
            [
              ["open", "待处理"],
              ["resolved", "已处理"],
              ["ignored", "已忽略"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              className={
                status === key
                  ? "rounded-full bg-slate-900 px-3 py-1 text-xs text-white"
                  : "rounded-full px-3 py-1 text-xs text-slate-500 hover:bg-slate-100"
              }
              onClick={() => {
                setStatus(key);
                setOffset(0);
              }}
            >
              {label}
            </button>
          ))}
          {FEEDBACK_FILTERS.map((item) => {
            const active = item.key === filter;
            return (
              <button
                key={item.key}
                type="button"
                aria-pressed={active}
                onClick={() => setFilter(item.key)}
                className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs transition-colors ${
                  active
                    ? "border-blue-500 bg-blue-50 font-medium text-blue-700"
                    : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                }`}
              >
                {item.label}
                <Badge tone={active ? "blue" : "slate"}>{counts[item.key]}</Badge>
              </button>
            );
          })}
          <span className="ml-auto text-[11px] text-slate-400">
            带正解的排在前面；同一句话只占一行
          </span>
        </div>

        <ul className="mt-3 overflow-hidden rounded-lg border border-slate-200">
          {visible.map(({ row, fix }) => (
            <li
              key={`${row.kind}-${row.question}-${row.code}-${row.resolution}`}
              className="flex items-start gap-4 border-b border-slate-100 px-4 py-3.5 last:border-b-0"
            >
              <div className="w-10 shrink-0 pt-0.5 text-center">
                <div className="text-lg font-semibold tabular-nums text-slate-700">
                  {row.count}
                </div>
                <div className="text-[10px] text-slate-400">次</div>
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-sm leading-relaxed text-slate-900">
                  「{row.question}」
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <Badge tone={KIND_TONE[row.kind]}>{KIND_LABEL[row.kind]}</Badge>
                  <span className="text-xs text-slate-500">{row.what}</span>
                </div>
                {fix && (
                  <div className="mt-2 text-xs text-slate-500">
                    落点：
                    <span className="font-medium text-slate-700">{fix.name}</span>
                    <span className="ml-1 text-slate-400">
                      （{TARGET_LABEL[fix.kind]}）
                    </span>
                  </div>
                )}
              </div>
              <div className="shrink-0 pt-0.5">
                {fix ? (
                  <Button
                    size="sm"
                    variant={readOnly ? "default" : "primary"}
                    disabled={readOnly}
                    icon={<BookOpenText className="h-3.5 w-3.5" />}
                    title={
                      readOnly
                        ? "当前版本只读，先在右上角基于此版本继续编辑"
                        : undefined
                    }
                    onClick={() => onFixInDictionary(fix)}
                  >
                    补进词典
                  </Button>
                ) : (
                  <span className="text-[11px] text-slate-400">要先诊断</span>
                )}
                {status === "open" && row.ids.length > 0 && (
                  <div className="mt-1.5 flex justify-end gap-2 text-[11px]">
                    {/* 处理过的收起来，不是删掉——这份数据还是补词典的依据。 */}
                    <button
                      type="button"
                      className="text-slate-500 hover:text-slate-700"
                      onClick={() => mark.mutate({ ids: row.ids, next: "resolved" })}
                    >
                      标为已处理
                    </button>
                    <button
                      type="button"
                      className="text-slate-400 hover:text-slate-600"
                      onClick={() => mark.mutate({ ids: row.ids, next: "ignored" })}
                    >
                      忽略
                    </button>
                  </div>
                )}
              </div>
            </li>
          ))}
          {visible.length === 0 && (
            <li className="px-4 py-8 text-center text-xs text-slate-400">
              这一类现在是空的。
            </li>
          )}
        </ul>

        {total > 0 && (
          <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
            {/* 说清"还剩多少条待处理"——那正是这个页面存在的意义。 */}
            <span>
              共 {total} 条，当前第 {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} 条
            </span>
            <span className="flex gap-2">
              <button
                type="button"
                className="disabled:text-slate-300"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                上一页
              </button>
              <button
                type="button"
                className="disabled:text-slate-300"
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                下一页
              </button>
            </span>
          </div>
        )}
      </section>

      <aside className="border-l border-slate-100 px-4 py-5">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          怎么处理
        </div>
        <ol className="rounded-lg border border-slate-200 p-3">
          {[
            "先看「用户替系统补了答案」——正解已经在那儿了，照着补别名即可。",
            "再看「模型自己猜的」里次数高的。猜对了也要补：下次可能猜错。",
            "「要先诊断」多半是关系路径缺失或事实根不对，回实体与关系改。",
          ].map((text, index) => (
            <li key={index} className="flex gap-2.5 py-1.5">
              <span className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full bg-blue-50 text-[10px] font-semibold text-blue-700">
                {index + 1}
              </span>
              <span className="text-[11px] leading-relaxed text-slate-500">
                {text}
              </span>
            </li>
          ))}
        </ol>

        <div className="mb-2 mt-5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          这一版的词典
        </div>
        <div className="rounded-lg border border-slate-200 px-3 py-2.5 text-xs text-slate-600">
          <div className="flex items-center justify-between">
            <span>业务术语</span>
            <span className="tabular-nums font-medium text-slate-800">
              {revision.semantic_catalog.terms.length}
            </span>
          </div>
          <div className="mt-1.5 flex items-center justify-between">
            <span>维度值</span>
            <span className="tabular-nums font-medium text-slate-800">
              {revision.semantic_catalog.dimensionValues.length}
            </span>
          </div>
        </div>

        <div className="mt-4 rounded-lg border border-dashed border-slate-300 px-3 py-2.5 text-[11px] leading-relaxed text-slate-500">
          补进词典写的是草稿版本，发布后才对线上生效。一次线上提问不是长期业务口径。
        </div>
      </aside>
    </div>
  );
}

function Header() {
  return (
    <header>
      <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
        <MessageSquareText className="h-4 w-4 text-slate-400" />
        用户说了什么，系统没接住
      </h2>
      <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-500">
        线上真实提问回流到建模。这一页只回答一个问题：下一版该补哪些说法。
      </p>
    </header>
  );
}

function Stat({
  label,
  value,
  unit,
  hint,
  tone = "slate",
}: {
  label: string;
  value: number;
  unit: string;
  hint?: string;
  tone?: "slate" | "amber";
}) {
  return (
    <div
      className={`rounded-lg border px-3.5 py-3 ${
        tone === "amber" ? "border-amber-200 bg-amber-50/50" : "border-slate-200"
      }`}
    >
      <div
        className={`text-[11px] ${tone === "amber" ? "text-amber-700" : "text-slate-400"}`}
      >
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span
          className={`text-xl font-semibold tabular-nums ${
            tone === "amber" ? "text-amber-800" : "text-slate-900"
          }`}
        >
          {value}
        </span>
        <span
          className={`text-[11px] ${tone === "amber" ? "text-amber-700" : "text-slate-400"}`}
        >
          {unit}
        </span>
        {hint && <span className="ml-1 text-[11px] text-slate-400">{hint}</span>}
      </div>
    </div>
  );
}
