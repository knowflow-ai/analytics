import { useMutation } from '@tanstack/react-query';
import {
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  Download,
  FileSearch,
  Loader2,
  MinusCircle,
  RotateCw,
  XCircle,
} from 'lucide-react';
import { useRef, useState, type ReactNode } from 'react';

import { exportQueryDiagnostic } from '@analytics/api/analytics';
import type {
  AnalyticsQueryDiagnosticExport,
  AnalyticsQueryDiagnosticStatus,
  AnalyticsQueryDiagnosticTimelineItem,
} from '@analytics/api/types';
import {
  Badge,
  Button,
  Dialog,
  ErrorBanner,
  Spinner,
  cx,
} from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

const STATUS: Record<
  AnalyticsQueryDiagnosticStatus,
  {
    label: string;
    tone: 'green' | 'red' | 'amber' | 'blue' | 'slate';
    icon: ReactNode;
    dot: string;
  }
> = {
  completed: {
    label: '已完成',
    tone: 'green',
    icon: <CheckCircle2 className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-emerald-200 bg-emerald-50 text-emerald-600',
  },
  failed: {
    label: '失败',
    tone: 'red',
    icon: <XCircle className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-red-200 bg-red-50 text-red-600',
  },
  clarification: {
    label: '待确认',
    tone: 'amber',
    icon: <CircleHelp className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-amber-200 bg-amber-50 text-amber-600',
  },
  started: {
    label: '已开始',
    tone: 'blue',
    icon: <Loader2 className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-blue-200 bg-blue-50 text-blue-600',
  },
  not_run: {
    label: '未运行',
    tone: 'slate',
    icon: <MinusCircle className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-slate-200 bg-slate-50 text-slate-400',
  },
  not_recorded: {
    label: '未记录',
    tone: 'slate',
    icon: <CircleDashed className="h-4 w-4" aria-hidden="true" />,
    dot: 'border-slate-200 bg-white text-slate-400',
  },
};

const HIDDEN_TOKEN_KEYS = new Set([
  'candidate_id',
  'candidate_ids',
  'selected_candidate_id',
  'selected_candidate_ids',
  'candidate_token',
  'candidate_tokens',
  'continuation_token',
]);

/** Defense in depth: exported continuation tokens are never useful to a reader. */
export function hideOpaqueDiagnosticTokens(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(hideOpaqueDiagnosticTokens);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      HIDDEN_TOKEN_KEYS.has(key.toLowerCase())
        ? '[已隐藏不透明确认令牌]'
        : hideOpaqueDiagnosticTokens(item),
    ]),
  );
}

function DiagnosticJson({ value }: { value: unknown }) {
  return (
    <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-slate-950 px-3 py-2 font-mono text-[10px] leading-4 text-slate-200">
      {JSON.stringify(hideOpaqueDiagnosticTokens(value), null, 2)}
    </pre>
  );
}

function TimelineStage({ item, last }: { item: AnalyticsQueryDiagnosticTimelineItem; last: boolean }) {
  const status = STATUS[item.status];
  const hasEvidence = item.events.length > 0 || Object.keys(item.artifacts).length > 0;
  const title = item.group === 'query' ? `${item.key} · ${item.label}` : item.label;
  return (
    <li className="relative grid grid-cols-[28px_minmax(0,1fr)] gap-3 pb-4 last:pb-0">
      {!last && <span className="absolute left-[13px] top-7 h-[calc(100%-1rem)] w-px bg-slate-200" aria-hidden="true" />}
      <span
        className={cx(
          'relative z-10 grid h-7 w-7 place-items-center rounded-full border',
          status.dot,
        )}
      >
        {status.icon}
      </span>
      <div className="min-w-0 pt-0.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-slate-700">{title}</span>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-5 text-slate-500">
          {item.summary || '没有阶段摘要。'}
        </p>
        {hasEvidence && (
          <details className="mt-1.5 rounded-md border border-slate-200 bg-white text-[11px]">
            <summary className="cursor-pointer select-none px-2.5 py-1.5 text-slate-500 hover:bg-slate-50">
              查看事件与产物
            </summary>
            <div className="border-t border-slate-100 px-2.5 py-2">
              {item.events.length > 0 && (
                <div>
                  <div className="font-medium text-slate-500">事件（按实际发生顺序）</div>
                  <DiagnosticJson value={item.events} />
                </div>
              )}
              {Object.keys(item.artifacts).length > 0 && (
                <div className={item.events.length > 0 ? 'mt-2' : undefined}>
                  <div className="font-medium text-slate-500">阶段产物</div>
                  <DiagnosticJson value={item.artifacts} />
                </div>
              )}
            </div>
          </details>
        )}
      </div>
    </li>
  );
}

function TimelineGroup({ title, items }: { title: string; items: AnalyticsQueryDiagnosticTimelineItem[] }) {
  return (
    <section>
      <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        {title}
      </h3>
      {items.length > 0 ? (
        <ol>
          {items.map((item, index) => (
            <TimelineStage key={item.key} item={item} last={index === items.length - 1} />
          ))}
        </ol>
      ) : (
        <p className="text-[11px] text-slate-400">服务端没有返回这一组诊断阶段。</p>
      )}
    </section>
  );
}

export function createDiagnosticMarkdownBlob(report: AnalyticsQueryDiagnosticExport): Blob {
  return new Blob([report.markdown], { type: report.media_type });
}

export interface DiagnosticDownloadEnvironment {
  createObjectUrl: (blob: Blob) => string;
  clickDownload: (url: string, filename: string) => void;
  defer: (callback: () => void) => void;
  revokeObjectUrl: (url: string) => void;
}

function browserDownloadEnvironment(): DiagnosticDownloadEnvironment {
  return {
    createObjectUrl: (blob) => window.URL.createObjectURL(blob),
    clickDownload: (url, filename) => {
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.style.display = 'none';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    },
    defer: (callback) => {
      window.setTimeout(callback, 0);
    },
    revokeObjectUrl: (url) => window.URL.revokeObjectURL(url),
  };
}

export function downloadDiagnosticMarkdown(
  report: AnalyticsQueryDiagnosticExport,
  environment: DiagnosticDownloadEnvironment = browserDownloadEnvironment(),
) {
  const url = environment.createObjectUrl(createDiagnosticMarkdownBlob(report));
  environment.clickDownload(url, report.filename);
  environment.defer(() => environment.revokeObjectUrl(url));
}

export async function runDiagnosticExportOnce<T>(
  inFlight: { current: boolean },
  task: () => Promise<T>,
): Promise<T | undefined> {
  if (inFlight.current) return undefined;
  inFlight.current = true;
  try {
    return await task();
  } finally {
    inFlight.current = false;
  }
}

export function QueryDiagnosticDialog({
  open,
  report,
  loading,
  error,
  onClose,
  onRetry,
  onDownload,
}: {
  open: boolean;
  report: AnalyticsQueryDiagnosticExport | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRetry: () => void;
  onDownload: () => void;
}) {
  const context = report?.timeline.filter((item) => item.group === 'context') ?? [];
  const query = report?.timeline.filter((item) => item.group === 'query') ?? [];
  const diagnosticCategory = report?.summary.category ?? report?.summary.diagnostic_category;
  return (
    <Dialog
      open={open}
      title="一键问数诊断"
      onClose={onClose}
      width="max-w-4xl"
      height="h-[88vh]"
      footer={
        report ? (
          <>
            <Button onClick={onClose}>关闭</Button>
            <Button
              variant="primary"
              icon={<Download className="h-3.5 w-3.5" aria-hidden="true" />}
              onClick={onDownload}
            >
              下载 Markdown
            </Button>
          </>
        ) : undefined
      }
    >
      {loading && <Spinner label="正在汇总本次问数的完整证据…" />}
      {!loading && error && (
        <div className="flex flex-col gap-3">
          <ErrorBanner message={error} />
          <p className="text-xs text-slate-500">
            诊断导出失败，原问数结果不受影响。可以稍后重试，不会重新执行原查询。
          </p>
          <div>
            <Button
              icon={<RotateCw className="h-3.5 w-3.5" aria-hidden="true" />}
              onClick={onRetry}
            >
              重试
            </Button>
          </div>
        </div>
      )}
      {!loading && report && (
        <div className="flex flex-col gap-5">
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-800">
            报告可能包含业务问题、语义目录、SQL 和结果样本，分享前请检查敏感信息。
          </div>
          <section className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={report.summary.state === 'FAILED' ? 'red' : report.summary.state === 'CLARIFICATION_REQUIRED' ? 'amber' : 'green'}>
                {report.summary.state}
              </Badge>
              <span className="font-mono text-[10px] text-slate-400">{report.summary.query_id}</span>
              {report.summary.version_status && (
                <Badge tone={report.summary.version_status === 'CURRENT' ? 'blue' : 'amber'}>
                  {report.summary.version_status}
                </Badge>
              )}
            </div>
            <div className="mt-2 text-xs font-medium text-slate-700">{report.summary.question}</div>
            {(report.summary.diagnostic_stage || diagnosticCategory) && (
              <div className="mt-1 text-[11px] text-slate-500">
                快速定位：{[report.summary.diagnostic_stage, diagnosticCategory].filter(Boolean).join(' · ')}
              </div>
            )}
            {report.summary.message && (
              <div className="mt-1 text-[11px] text-slate-600">{report.summary.message}</div>
            )}
          </section>
          <TimelineGroup title="建模上下文（非本次查询）" items={context} />
          <TimelineGroup title="本次查询" items={query} />
        </div>
      )}
    </Dialog>
  );
}

export function QueryDiagnosticAction({ projectId, queryId }: { projectId: string; queryId: string }) {
  const [open, setOpen] = useState(false);
  const inFlight = useRef(false);
  const exportRequest = useMutation({
    mutationFn: () => exportQueryDiagnostic(projectId, queryId),
  });
  const load = () => {
    void runDiagnosticExportOnce(inFlight, () => exportRequest.mutateAsync()).catch(
      () => undefined,
    );
  };
  const show = () => {
    setOpen(true);
    if (!exportRequest.data) load();
  };
  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        loading={exportRequest.isPending}
        icon={<FileSearch className="h-3.5 w-3.5" aria-hidden="true" />}
        aria-haspopup="dialog"
        onClick={show}
      >
        {exportRequest.isPending ? '正在生成诊断…' : '一键诊断'}
      </Button>
      <QueryDiagnosticDialog
        open={open}
        report={exportRequest.data ?? null}
        loading={exportRequest.isPending}
        error={exportRequest.error ? describeError(exportRequest.error) : null}
        onClose={() => setOpen(false)}
        onRetry={load}
        onDownload={() => {
          if (exportRequest.data) downloadDiagnosticMarkdown(exportRequest.data);
        }}
      />
    </>
  );
}
