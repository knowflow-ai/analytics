import { ChevronDown } from 'lucide-react';
import { useState } from 'react';
import type {
  AnalyticsClarificationOption,
  AnalyticsCompletedQueryResponse,
  AnalyticsQueryResponse,
} from '@analytics/api/types';
import type { QueryInput, RevisionVersion } from '@analytics/api/analytics';
import { Badge, Spinner } from '@analytics/components/ui';
import { QueryDiagnosticAction } from '@analytics/components/query-diagnostics';

export interface QueryTurn {
  id: string;
  question: string;
  /** Exact request sent for this turn; clarification must continue from it. */
  input?: QueryInput;
  response?: AnalyticsQueryResponse;
  error?: string;
  pending?: boolean;
  target?: QueryTurnTarget;
}

export type QueryTurnTarget =
  | { mode: 'release' }
  | { mode: 'draft'; revisionId: string; version: RevisionVersion };

/**
 * Continue from the request that produced the clarification.
 *
 * Candidate ids are release-bound opaque tokens. In particular, an analysis
 * object token may already carry a preceding element/value confirmation, so
 * clients must return the current token untouched and must not reconstruct or
 * inspect it. Keeping the originating dataset set prevents a later card click
 * from reopening business paths that an earlier step already constrained.
 */
export function buildClarificationContinuation(
  origin: QueryInput,
  option: AnalyticsClarificationOption,
  response: AnalyticsQueryResponse,
): QueryInput {
  return {
    ...origin,
    dataset_ids: [...origin.dataset_ids],
    selected_candidate_id: option.candidate_id,
    expected_release_id: response.release_id,
    expected_spec_hash: response.spec_hash,
    expected_index_snapshot_id: response.index_snapshot_id ?? undefined,
  };
}

const CLARIFICATION_KIND = {
  metric: { label: '指标', tone: 'green' },
  dimension: { label: '维度', tone: 'blue' },
  dimension_value: { label: '维度值', tone: 'amber' },
  analysis_object: { label: '分析对象', tone: 'violet' },
} as const satisfies Record<
  AnalyticsClarificationOption['kind'],
  { label: string; tone: 'green' | 'blue' | 'amber' | 'violet' }
>;

function DataTable({
  data,
  labels,
  columnName,
  exposeTechnical,
}: {
  data: AnalyticsCompletedQueryResponse['data'];
  /** 后端逐位给的展示名；计算列（占比等）只有它知道叫什么，id 末段是「0」。 */
  labels?: string[];
  columnName: (id: string) => string;
  exposeTechnical: boolean;
}) {
  const headerOf = (column: string, index: number) => labels?.[index] || columnName(column);
  if (!data.rows.length) return <div className="py-3 text-xs text-[var(--kf-text-tertiary)]">查询成功，但没有返回数据。</div>;
  return (
    <div className="overflow-x-auto rounded-md border border-[var(--kf-border-secondary)]">
      <table className="w-full text-left text-xs">
        <thead className="bg-[rgb(var(--kf-fill-alter-rgb))] text-xs uppercase tracking-wide text-[var(--kf-text-secondary)]">
          <tr>
            {data.columns.map((column, index) => (
              <th
                key={column}
                className="whitespace-nowrap px-3 py-2 font-medium"
                title={exposeTechnical ? column : headerOf(column, index)}
              >
                {headerOf(column, index)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--kf-border-secondary)]">
          {data.rows.map((row, index) => (
            <tr key={index} className="hover:bg-[rgb(var(--kf-fill-alter-rgb))]">
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="whitespace-nowrap px-3 py-1.5 text-[var(--kf-text)]">
                  {cell === null || cell === undefined ? <span className="text-[var(--kf-text-quaternary)]">—</span> : String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-[var(--kf-border-secondary)] px-3 py-1.5 text-xs text-[var(--kf-text-tertiary)]">
        {data.row_count} 行{data.truncated ? '（已截断）' : ''}
      </div>
    </div>
  );
}

function Collapsible({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-[var(--kf-border-secondary)]">
      <button
        type="button"
        className="flex w-full items-center justify-between px-3 py-1.5 text-xs font-medium text-[var(--kf-text-secondary)] hover:bg-[rgb(var(--kf-fill-alter-rgb))]"
        onClick={() => setOpen(!open)}
      >
        {title}
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && <div className="border-t border-[var(--kf-border-secondary)] px-3 py-2">{children}</div>}
    </div>
  );
}

export function QueryAnswer({
  projectId,
  turn,
  columnName,
  onChoose,
}: {
  projectId: string;
  turn: QueryTurn;
  columnName: (id: string) => string;
  onChoose: (option: AnalyticsClarificationOption, response: AnalyticsQueryResponse) => void;
}) {
  if (turn.pending) return <Spinner label="正在理解问题并查询…" />;
  if (turn.error) return <div className="text-xs text-[var(--kf-error-text)]">{turn.error}</div>;
  const response = turn.response!;
  if (response.state === 'CLARIFICATION_REQUIRED') {
    return (
      <div className="flex flex-col gap-2">
        <div className="text-xs text-[var(--kf-text)]">{response.question}</div>
        <div role="group" aria-label="可选业务语义" className="flex flex-col gap-2">
          {response.options.map((option) => {
            const presentation = CLARIFICATION_KIND[option.kind];
            return (
              <button
                key={option.candidate_id}
                type="button"
                className="flex w-full min-w-0 flex-col items-start gap-1.5 rounded-lg border border-[var(--kf-border-secondary)] bg-[var(--kf-bg-container)] px-3 py-2.5 text-left transition-colors hover:border-[var(--kf-primary-border)] hover:bg-[rgb(var(--kf-primary-bg-rgb)/0.4)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--kf-primary-border)]"
                onClick={() => onChoose(option, response)}
              >
                <span className="flex w-full min-w-0 items-start gap-2">
                  <Badge tone={presentation.tone}>{presentation.label}</Badge>
                  <span className="min-w-0 flex-1 whitespace-normal break-words text-xs font-medium leading-5 text-[var(--kf-text)]">
                    {option.label}
                  </span>
                </span>
                {option.description && (
                  <span className="w-full whitespace-normal break-words text-xs leading-relaxed text-[var(--kf-text-secondary)]">
                    {option.description}
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <div className="flex justify-end">
          <QueryDiagnosticAction projectId={projectId} queryId={response.query_id} />
        </div>
      </div>
    );
  }
  if (response.state === 'FAILED') {
    // 后端的 error.message 已经点名具体原因(哪个值丢在哪个维度、同名冲突等),
    // 它是主信息;user_hint 是给提问者的下一步,作为补充展示,不再覆盖真因。
    const failedStep = [...response.trace].reverse().find((s) => s.status === 'failed');
    const detail = failedStep?.detail ?? {};
    const rejectedSql = typeof detail.rejected_s2sql === 'string' ? detail.rejected_s2sql : null;
    const missing = Array.isArray(detail.missing_values)
      ? (detail.missing_values as Array<{ dimension_name?: string; value?: string }>)
      : [];
    return (
      <div className="flex flex-col gap-1.5 text-xs">
        <div className="text-[var(--kf-error-text)]">{response.error.message}</div>
        {response.diagnostics?.user_hint && response.diagnostics.user_hint !== response.error.message && (
          <div className="text-xs text-[var(--kf-text-secondary)]">{response.diagnostics.user_hint}</div>
        )}
        {missing.length > 0 && (
          <div className="text-xs text-[var(--kf-text-secondary)]">
            未落地的精确值：
            {missing.map((m, i) => (
              <span key={i} className="ml-1 rounded bg-[var(--kf-error-bg)] px-1.5 py-0.5 text-[var(--kf-error-text)]">
                「{m.value}」@ {m.dimension_name}
              </span>
            ))}
          </div>
        )}
        {rejectedSql && (
          <details className="text-xs">
            <summary className="cursor-pointer text-[var(--kf-text-secondary)]">被拒的语义 SQL</summary>
            <pre className="mt-1 overflow-x-auto rounded bg-[rgb(var(--kf-fill-alter-rgb))] p-2 font-mono text-xs text-[var(--kf-text)]">{rejectedSql}</pre>
          </details>
        )}
        <details className="text-xs">
          <summary className="cursor-pointer text-[var(--kf-text-tertiary)]">
            阶段 {response.error.stage} · {response.error.code} · 各阶段轨迹
          </summary>
          <div className="mt-1 flex flex-col gap-0.5">
            {response.trace.map((s, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className={s.status === 'failed' ? 'text-[var(--kf-error-text)]' : s.status === 'completed' ? 'text-[var(--kf-success-text)]' : 'text-[var(--kf-text-tertiary)]'}>
                  {s.status === 'failed' ? '✗' : s.status === 'completed' ? '✓' : '…'}
                </span>
                <span className="text-[var(--kf-text-secondary)]">{s.stage}</span>
              </div>
            ))}
          </div>
        </details>
        {response.diagnostics?.recommendation && (
          <div className="text-xs text-[var(--kf-text-tertiary)]">建模建议：{response.diagnostics.recommendation}</div>
        )}
        <div className="flex justify-end">
          <QueryDiagnosticAction projectId={projectId} queryId={response.query_id} />
        </div>
      </div>
    );
  }
  const { interpretation } = response;
  const exposeTechnical = turn.target?.mode === 'draft';
  const semanticDecisions =
    response.semantic_decisions ??
    (response.resolved_by_llm ?? []).map((item) => ({
      ...item,
      source: 'final_llm' as const,
    }));
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5 text-xs">
          {interpretation.metrics.map((m) => (
            <Badge key={m} tone="green">{m}</Badge>
          ))}
          {interpretation.dimensions.map((d) => (
            <Badge key={d}>按 {d}</Badge>
          ))}
          {interpretation.filters.map((f) => (
            <Badge key={f} tone="amber">{f}</Badge>
          ))}
          {interpretation.applied_defaults.map((d) => (
            <span key={d} className="text-[var(--kf-text-tertiary)]">默认 {d}</span>
          ))}
        </div>
        <QueryDiagnosticAction projectId={projectId} queryId={response.query_id} />
      </div>
      {semanticDecisions.length > 0 && (
        <div className="flex flex-wrap gap-2" aria-label="自动业务理解">
          {semanticDecisions.map((item, index) => (
            <div
              key={`${item.source}:${item.detected_text}:${index}`}
              className="flex flex-wrap items-center gap-1.5 rounded-full border border-violet-100 bg-violet-50 px-2.5 py-1.5 text-xs text-violet-800"
            >
              {item.chosen.kind === 'analysis_object' ? (
                <span>按{item.chosen.label}分析</span>
              ) : (
                <span>
                  {item.source === 'memory' ? '沿用确认' : item.source === 'human' ? '已确认' : '自动理解'}
                  「{item.detected_text}」为 <b>{item.chosen.label}</b>
                </span>
              )}
              {item.alternatives.map((alternative) => (
                <button
                  key={alternative.candidate_id}
                  type="button"
                  className="rounded-full border border-violet-200 bg-[var(--kf-bg-container)] px-1.5 py-0.5 hover:border-violet-300 hover:text-violet-950"
                  onClick={() => onChoose(alternative, response)}
                >
                  切换为「{alternative.label}」
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
      <DataTable
        data={response.data}
        labels={response.column_labels}
        columnName={columnName}
        exposeTechnical={exposeTechnical}
      />
      {exposeTechnical && (
        <Collapsible title="查看 S2SQL 与物理 SQL">
          <pre className="whitespace-pre-wrap break-all font-mono text-xs text-[var(--kf-text-secondary)]">{response.corrected_s2sql}</pre>
          {response.physical_sql && (
            <pre className="mt-2 whitespace-pre-wrap break-all border-t border-[var(--kf-border-secondary)] pt-2 font-mono text-xs text-[var(--kf-text-secondary)]">
              {response.physical_sql}
            </pre>
          )}
        </Collapsible>
      )}
    </div>
  );
}
