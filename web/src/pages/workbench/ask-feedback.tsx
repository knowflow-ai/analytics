import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  listConfirmationSuggestions,
  listQueryFailures,
  saveDimension,
  saveMetric,
  versionOf,
} from "@analytics/api/analytics";
import { Badge, Button, Empty, ErrorBanner, Spinner, useToast } from "@analytics/components/ui";
import { describeError } from "@analytics/lib/labels";
import {
  adoptableSuggestions,
  groupFailures,
  withAlias,
  type AdoptableSuggestion,
  type AliasTarget,
} from "./ask-feedback-state";
import type { WorkbenchContext } from "./index";

const splitAliases = (value: string | null | undefined) =>
  value
    ? value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    : [];

type Context = Pick<
  WorkbenchContext,
  "projectId" | "revision" | "acceptRevision" | "readOnly"
>;

/**
 * 问数反馈：线上真实提问回流到建模。
 *
 * 系统一直在攒证据——被拒答的问题、用户反复做的人工确认——但此前没有任何
 * 页面消费它们，"用户说了什么、系统听不懂什么"只能靠人凭印象回忆。这里只做
 * 两件事：把证据按可操作性排好，以及把一次确认变成一条别名草稿。
 *
 * 治理边界：采纳只写当前草稿 Revision，发布仍走既有审核流程；一次线上选择
 * 不是长期业务事实，不允许直接改 Active Release。
 */
export function AskFeedbackPanel({
  projectId,
  revision,
  acceptRevision,
  readOnly,
}: Context) {
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);

  const failures = useQuery({
    queryKey: ["analytics-query-failures", projectId],
    queryFn: () => listQueryFailures(projectId, 200),
  });
  const suggestions = useQuery({
    queryKey: ["analytics-confirmation-suggestions", projectId],
    queryFn: () => listConfirmationSuggestions(projectId),
  });

  const catalog = revision.semantic_catalog;
  const aliasTargets = useMemo<AliasTarget[]>(
    () => [
      ...(catalog?.metrics ?? []).map((item) => ({
        kind: "metric" as const,
        id: item.id,
        name: item.name,
        aliases: splitAliases(item.alias),
      })),
      ...(catalog?.dimensions ?? []).map((item) => ({
        kind: "dimension" as const,
        id: item.id,
        name: item.name,
        aliases: splitAliases(item.alias),
      })),
    ],
    [catalog],
  );

  const failureRows = useMemo(
    () => groupFailures(failures.data?.items ?? []),
    [failures.data],
  );
  const suggestionRows = useMemo(
    () => adoptableSuggestions(suggestions.data?.items ?? [], aliasTargets),
    [suggestions.data, aliasTargets],
  );

  const adopt = useMutation({
    mutationFn: async ({
      target,
      alias,
    }: {
      target: AliasTarget;
      alias: string;
    }) => {
      const next = withAlias(target, alias);
      if (next === target.aliases) return null;
      const joined = next.join(",");
      // 整份目录 DTO 原样回写、只覆写 alias：重建 DTO 会丢掉这里没覆盖的键。
      if (target.kind === "metric") {
        const metric = (catalog?.metrics ?? []).find(
          (item) => item.id === target.id,
        );
        if (!metric) return null;
        return saveMetric(projectId, revision.id, versionOf(revision), {
          ...metric,
          alias: joined,
        });
      }
      const dimension = (catalog?.dimensions ?? []).find(
        (item) => item.id === target.id,
      );
      if (!dimension) return null;
      return saveDimension(projectId, revision.id, versionOf(revision), {
        ...dimension,
        alias: joined,
      });
    },
    onSuccess: (next, variables) => {
      if (!next) return;
      acceptRevision(next);
      setError(null);
      toast.success(
        `已把「${variables.alias}」加为「${variables.target.name}」的别名`,
      );
    },
    onError: (cause) => setError(describeError(cause)),
  });

  return (
    <div className="space-y-4">
      {error && <ErrorBanner message={error} />}

      <p className="rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-slate-600">
        这里的采纳只写入当前草稿版本。一次线上确认不等于长期业务口径，仍需经过
        发布前校验与发布流程，已发布版本不会被直接修改。
      </p>

      <section>
        <header className="mb-2 flex items-baseline justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-800">
            用户反复确认的说法
          </h3>
          <span className="text-xs text-slate-400">
            同一说法被多次人工确认，说明它就是用户的常用叫法
          </span>
        </header>
        {suggestions.isPending ? (
          <Spinner label="加载中" />
        ) : suggestionRows.length ? (
          <SuggestionTable
            rows={suggestionRows}
            readOnly={readOnly}
            adopting={adopt.isPending}
            onAdopt={(target, alias) => adopt.mutate({ target, alias })}
          />
        ) : (
          <Empty
            title="还没有重复确认记录"
            hint="用户在问数里做过澄清选择后，这里会出现待采纳的说法。"
          />
        )}
      </section>

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

function SuggestionTable({
  rows,
  readOnly,
  adopting,
  onAdopt,
}: {
  rows: AdoptableSuggestion[];
  readOnly: boolean;
  adopting: boolean;
  onAdopt: (target: AliasTarget, alias: string) => void;
}) {
  return (
    <div className="overflow-auto rounded-lg border border-slate-200">
      <table className="w-full min-w-[680px] text-left text-xs">
        <thead className="bg-slate-50 text-[11px] text-slate-400">
          <tr>
            <th className="px-3 py-2 font-medium">用户的说法</th>
            <th className="px-3 py-2 font-medium">确认次数</th>
            <th className="px-3 py-2 font-medium">用户当时选的是</th>
            <th className="px-3 py-2 font-medium">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="px-3 py-2 font-medium text-slate-700">
                {row.detectedText}
              </td>
              <td className="px-3 py-2 tabular-nums text-slate-600">
                {row.count}
              </td>
              <td className="px-3 py-2">
                {row.target ? (
                  <span className="inline-flex items-center gap-1.5">
                    <Badge tone={row.target.kind === "metric" ? "green" : "blue"}>
                      {row.target.kind === "metric" ? "指标" : "维度"}
                    </Badge>
                    <span className="text-slate-700">{row.target.name}</span>
                  </span>
                ) : (
                  <span className="text-slate-400">目标已不在当前目录中</span>
                )}
              </td>
              <td className="px-3 py-2">
                {row.alreadyCovered ? (
                  <span className="text-slate-400">已是别名</span>
                ) : row.target ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={readOnly || adopting}
                    onClick={() => onAdopt(row.target!, row.detectedText)}
                  >
                    采纳为别名
                  </Button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
