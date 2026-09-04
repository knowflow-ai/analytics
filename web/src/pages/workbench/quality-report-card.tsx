import { AlertTriangle, CheckCircle2, Database, Info } from 'lucide-react';
import type {
  AnalyticsModelingQualityReport,
  AnalyticsQualityStatus,
} from '@analytics/api/types';
import { Badge } from '@analytics/components/ui';

/**
 * 数据质量报告：发布前用真实只读数据核对的证据。
 *
 * 结构校验只能证明模型自洽；这里回答的是「声明与真实数据符不符」——主标识唯一率、
 * 关系实测基数、指标样本值、指标—维度可达性。
 *
 * 这张卡**只呈现证据，不带任何按钮**：报告由「问数验证」在进入这一步时自动跑
 * （见 `publish-checks.ts`），需要人拍板的那几条也已经提到上面的「需要你确认」
 * 队列里。两处都放确认入口会让人不知道该点哪一个，也会让同一份报告出现两种说法。
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

/**
 * 行级摘要。
 *
 * 只列「提醒」这一类:阻断、待核对、已拒绝都已经被提到上面的「需要你确认」队列里,
 * 这里再列一遍就是同一条证据出现两次——实机截图里 6 条待核对的指标样本，队列里一遍、
 * 展开区又一遍，用户会以为是两批不同的东西。它们只在这里留一个计数。
 */
function splitRows<T extends { status: AnalyticsQualityStatus }>(rows: T[]) {
  const attention = rows.filter((row) => row.status === 'warning');
  const queued = rows.filter(
    (row) => row.status !== 'passed' && row.status !== 'confirmed' && row.status !== 'warning',
  );
  return { attention, queued, passedCount: rows.length - attention.length - queued.length };
}

export function QualityReportCard({
  report,
  names,
}: {
  report: AnalyticsModelingQualityReport | null;
  /** 语义 id → 业务名;报告里的 id 对用户不可读。 */
  names: Map<string, string>;
}) {
  const label = (id: string) => names.get(id) ?? id.split(':').pop() ?? id;

  if (!report) {
    return (
      <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-xs text-slate-400">
        这一版还没有数据质量报告。结构校验通过后会自动跑一次。
      </div>
    );
  }

  // 未配置主标识不是数据质量发现,是这一项查不了的前置条件——结构诊断已经单独报过
  // 它并给了修复建议。两处都渲染会让用户修好一处后发现另一处还在。
  const allGrains = report.model_grains;
  const skippedGrains = allGrains.filter((item) => item.identifier_field_ids.length === 0);
  const checkedGrains = allGrains.filter((item) => item.identifier_field_ids.length > 0);

  return (
    <div className="rounded-lg border border-slate-200 p-3.5">
      <h3 className="flex items-center gap-2 text-xs font-semibold text-slate-800">
        <Database className="h-3.5 w-3.5 text-slate-400" /> 数据质量
      </h3>

      <div className="mt-3 flex flex-col gap-4 text-xs">
        <div className="flex items-center gap-2">
          {report.blocking_count > 0 ? (
            <AlertTriangle className="h-4 w-4 text-red-600" />
          ) : (
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
          )}
          <span className="font-medium text-slate-800">
            {report.blocking_count > 0 ? `${report.blocking_count} 个阻断问题` : '没有阻断问题'}
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

        <QualitySection title="指标样本" rows={report.metric_previews}>
          {(row) => (
            <>
              <span className="font-medium text-slate-700">{label(row.metric_id)}</span>
              <span className="font-mono text-slate-600">
                {row.rows?.[0]?.map((value) => String(value)).join(' , ') ?? '—'}
              </span>
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
    </div>
  );
}

function QualitySection<T extends { status: AnalyticsQualityStatus; message: string }>({
  title,
  rows,
  children,
  note,
}: {
  title: string;
  rows: T[];
  children: (row: T) => React.ReactNode;
  /** 被跳过而非「发现」的项,只做小结不占版面。 */
  note?: string;
}) {
  const { attention, queued, passedCount } = splitRows(rows);
  if (rows.length === 0 && !note) return null;
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          {title}
        </span>
        {attention.length === 0 && queued.length === 0 ? (
          <span className="text-[11px] text-emerald-600">全部通过（{rows.length}）</span>
        ) : (
          <span className="text-[11px] text-slate-400">
            {attention.length > 0 && `${attention.length} 项提醒`}
            {attention.length > 0 && queued.length > 0 && ' · '}
            {queued.length > 0 && `${queued.length} 项已在上面等你确认`}
            {passedCount > 0 && ` · ${passedCount} 项通过`}
          </span>
        )}
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
            {row.message && (
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
