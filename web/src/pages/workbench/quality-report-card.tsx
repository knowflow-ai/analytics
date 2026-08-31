import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, Database, Info } from 'lucide-react';
import {
  createQualityReport,
  getCurrentQualityReport,
  reviewQualityReport,
  versionOf,
} from '@analytics/api/analytics';
import type {
  AnalyticsModelingQualityReport,
  AnalyticsQualityStatus,
  AnalyticsRevision,
} from '@analytics/api/types';
import { Badge, Button, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

/**
 * 数据质量报告:发布前用真实只读数据核对。
 *
 * 结构校验只能证明模型自洽;这里回答的是「声明与真实数据符不符」——主标识
 * 唯一率、关系实测基数(与声明不符即阻断)、指标样本值、指标—维度可达性。
 * 报告只提供证据,不自动改写模型;独立版默认不强制通过才能发布,但必须看得见。
 */

const STATUS_TONE: Record<string, 'green' | 'red' | 'amber' | 'slate'> = {
  passed: 'green',
  blocking: 'red',
  rejected: 'red',
  warning: 'amber',
  pending_review: 'amber',
  confirmed: 'green',
};

const STATUS_LABEL: Record<string, string> = {
  passed: '通过',
  blocking: '阻断',
  warning: '提醒',
  pending_review: '待核对',
  confirmed: '已确认',
  rejected: '已拒绝',
};

function StatusBadge({ status }: { status: AnalyticsQualityStatus }) {
  return <Badge tone={STATUS_TONE[status] ?? 'slate'}>{STATUS_LABEL[status] ?? status}</Badge>;
}

const pct = (value: number) => `${(value * 100).toFixed(value >= 0.995 && value < 1 ? 1 : 0)}%`;

/** 行级摘要:非通过行排前面,通过行折叠成一句话。 */
function splitRows<T extends { status: AnalyticsQualityStatus }>(rows: T[]) {
  const attention = rows.filter((row) => row.status !== 'passed' && row.status !== 'confirmed');
  return { attention, passedCount: rows.length - attention.length };
}

export function QualityReportCard({
  projectId,
  revision,
  names,
  readOnly,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  /** 语义 id → 业务名;报告里的 id 对用户不可读。 */
  names: Map<string, string>;
  readOnly: boolean;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  // 报告从服务端读,不再只存局部 state:此前刷新即丢,发布按钮永远读不到质量
  // 状态,只能凭 revision.state 点亮——而后端用同一份证据拒绝发布。
  const queryKey = ['quality-report', projectId, revision.id, revision.etag];
  const current = useQuery({
    queryKey,
    queryFn: () => getCurrentQualityReport(projectId, revision.id),
  });
  const report = current.data?.report ?? null;
  const refresh = (next: AnalyticsModelingQualityReport) =>
    queryClient.setQueryData(queryKey, { report: next });
  const run = useMutation({
    mutationFn: () => createQualityReport(projectId, revision.id, versionOf(revision)),
    onSuccess: refresh,
    onError: (error) => toast.error(describeError(error)),
  });
  // 指标样本必须人工核对才算证据成立(ready = 无阻断 且 无待核对)。此前界面
  // 只显示「待核对」却没有任何确认入口,4 个 pending_review 永远解不掉,发布被
  // 死锁——重新核对也没用,重跑还是待核对。
  const review = useMutation({
    mutationFn: (decisions: Array<{ preview_id: string; confirm: boolean }>) =>
      reviewQualityReport(projectId, revision.id, report!, decisions),
    onSuccess: (next) => {
      refresh(next);
      toast.success('已记录核对结果。');
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const label = (id: string) => names.get(id) ?? id.split(':').pop() ?? id;
  const pendingPreviews = (report?.metric_previews ?? []).filter(
    (item) => item.status === 'pending_review',
  );
  // 未配置主标识不是数据质量发现,是这一项查不了的前置条件——结构诊断已经
  // 单独报过它并给了修复建议。两处都渲染会让用户修好一处后发现另一处还在
  // (要重跑核对才消失),所以这里只做「已跳过」的小结。
  const allGrains = report?.model_grains ?? [];
  const skippedGrains = allGrains.filter((item) => item.identifier_field_ids.length === 0);
  const checkedGrains = allGrains.filter((item) => item.identifier_field_ids.length > 0);

  return (
    <div className="mt-6 rounded-lg border border-slate-200 p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <Database className="h-4 w-4 text-slate-400" /> 数据质量
          </h3>
          <p className="mt-0.5 text-xs text-slate-500">
            用真实数据核对主标识唯一率、关系基数、指标样本和可达性;声明与数据不符会在这里现形。
          </p>
        </div>
        <Button
          variant="default"
          loading={run.isPending}
          disabled={readOnly}
          onClick={() => run.mutate()}
        >
          {report ? '重新核对' : '开始核对'}
        </Button>
      </div>

      {(run.isPending || current.isPending) && (
        <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
          <Spinner /> 正在对数据库执行只读检查…
        </div>
      )}

      {report && !run.isPending && (
        <div className="mt-4 flex flex-col gap-4 text-xs">
          <div className="flex items-center gap-2">
            {report.blocking_count > 0 ? (
              <AlertTriangle className="h-4 w-4 text-red-600" />
            ) : (
              <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            )}
            <span className="font-medium text-slate-800">
              {report.blocking_count > 0
                ? `${report.blocking_count} 个阻断问题`
                : '没有阻断问题'}
            </span>
            {report.warning_count > 0 && (
              <span className="text-slate-500">· {report.warning_count} 条提醒</span>
            )}
          </div>

          <QualitySection
            title="主标识唯一性"
            rows={checkedGrains}
            note={
              skippedGrains.length > 0
                ? `${skippedGrains.length} 个模型未配置主标识，已跳过（见上方诊断）`
                : undefined
            }
          >
            {(row) => (
              <>
                <span className="font-medium text-slate-700">{label(row.model_id)}</span>
                <span className="text-slate-500">
                  {row.total_rows} 行 · 唯一率 {pct(row.uniqueness_rate)}
                  {row.duplicate_rows > 0 && ` · 重复 ${row.duplicate_rows}`}
                  {row.null_rows > 0 && ` · 空值 ${row.null_rows}`}
                </span>
              </>
            )}
          </QualitySection>

          <QualitySection title="关系实测" rows={report.relations}>
            {(row) => (
              <>
                <span className="font-medium text-slate-700">{label(row.relation_id)}</span>
                <span className="text-slate-500">
                  声明 {row.declared_cardinality}
                  {row.observed_cardinality && row.observed_cardinality !== row.declared_cardinality
                    ? ` · 实测 ${row.observed_cardinality}`
                    : ''}
                  {row.orphan_left_rows + row.orphan_right_rows > 0 &&
                    ` · 未匹配 ${row.orphan_left_rows}/${row.orphan_right_rows}`}
                </span>
              </>
            )}
          </QualitySection>

          <QualitySection
            title="指标样本"
            rows={report.metric_previews}
            action={
              pendingPreviews.length > 0 && !readOnly ? (
                <Button
                  variant="default"
                  loading={review.isPending}
                  onClick={() =>
                    review.mutate(
                      pendingPreviews.map((item) => ({ preview_id: item.id, confirm: true })),
                    )
                  }
                >
                  确认全部 {pendingPreviews.length} 项
                </Button>
              ) : null
            }
          >
            {(row) => (
              <>
                <span className="font-medium text-slate-700">{label(row.metric_id)}</span>
                <span className="font-mono text-slate-600">
                  {row.rows?.[0]?.map((value) => String(value)).join(' , ') ?? '—'}
                </span>
                {row.status === 'pending_review' && !readOnly && (
                  <span className="flex shrink-0 gap-2">
                    <button
                      type="button"
                      className="text-blue-600 hover:text-blue-500"
                      disabled={review.isPending}
                      onClick={() => review.mutate([{ preview_id: row.id, confirm: true }])}
                    >
                      数值正确
                    </button>
                    <button
                      type="button"
                      className="text-red-600 hover:text-red-500"
                      disabled={review.isPending}
                      onClick={() => review.mutate([{ preview_id: row.id, confirm: false }])}
                    >
                      不对
                    </button>
                  </span>
                )}
              </>
            )}
          </QualitySection>

          <QualitySection title="指标 → 维度可达性" rows={report.reachability}>
            {(row) => (
              <>
                <span className="font-medium text-slate-700">
                  {label(row.metric_id)} × {label(row.dimension_id)}
                </span>
                <span className="text-slate-500">{row.message}</span>
              </>
            )}
          </QualitySection>
        </div>
      )}
    </div>
  );
}

function QualitySection<T extends { status: AnalyticsQualityStatus; message: string }>({
  title,
  rows,
  children,
  action,
  note,
}: {
  title: string;
  rows: T[];
  children: (row: T) => React.ReactNode;
  action?: React.ReactNode;
  /** 被跳过而非「发现」的项,只做小结不占版面。 */
  note?: string;
}) {
  const { attention, passedCount } = splitRows(rows);
  if (rows.length === 0 && !note) return null;
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          {title}
        </span>
        {attention.length === 0 ? (
          <span className="text-[11px] text-emerald-600">全部通过（{rows.length}）</span>
        ) : (
          <span className="text-[11px] text-slate-400">
            {attention.length} 项需注意 · {passedCount} 项通过
          </span>
        )}
        {action ? <span className="ml-auto">{action}</span> : null}
      </div>
      {note ? <div className="mb-1.5 text-[11px] text-slate-400">{note}</div> : null}
      <div className="flex flex-col gap-1.5">
        {attention.map((row, index) => (
          <div
            key={index}
            className={`flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 ${
              row.status === 'blocking' || row.status === 'rejected'
                ? 'border-red-200 bg-red-50/50'
                : 'border-amber-200/70 bg-amber-50/40'
            }`}
          >
            <StatusBadge status={row.status} />
            {children(row)}
            {row.status !== 'pending_review' && row.message && (
              <span className="flex items-center gap-1 text-slate-500">
                <Info className="h-3 w-3" /> {row.message}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
