import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { AnalyticsQueryDiagnosticExport } from '@analytics/api/types';
import {
  QueryDiagnosticDialog,
  createDiagnosticMarkdownBlob,
  downloadDiagnosticMarkdown,
  runDiagnosticExportOnce,
} from './query-diagnostics';

const REPORT: AnalyticsQueryDiagnosticExport = {
  filename: 'knowflow-diagnostic-query-1.md',
  media_type: 'text/markdown; charset=utf-8',
  markdown: '# KnowFlow 问数诊断\n\n完整报告',
  sha256: 'sha256:diagnostic',
  summary: {
    query_id: 'query-1',
    state: 'FAILED',
    mode: 'natural',
    question: '各届数的获奖数量',
    diagnostic_stage: 'ROUTE_BINDING',
    diagnostic_category: 'routing',
    message: '没有安全分析主题',
    version_status: 'CURRENT',
  },
  timeline: [
    {
      key: 'modeling',
      label: '数据源与 AI 建模',
      group: 'context',
      status: 'completed',
      summary: '6 个模型，5 个分析主题',
      events: [],
      artifacts: { proposal_count: 41 },
    },
    {
      key: 'revision',
      label: 'Revision / Release 冻结版本',
      group: 'context',
      status: 'completed',
      summary: 'revision-1',
      events: [],
      artifacts: {},
    },
    {
      key: 'PRECHECK',
      label: '请求预检',
      group: 'query',
      status: 'completed',
      summary: '版本绑定通过',
      events: [{ status: 'completed', detail: { release_id: 'release-1' } }],
      artifacts: {},
    },
    {
      key: 'CANDIDATE_DISCOVERY',
      label: '候选发现',
      group: 'query',
      status: 'clarification',
      summary: '需要确认业务对象',
      events: [
        {
          status: 'clarification',
          detail: {
            candidate_id: 'opaque-secret-candidate-token',
            option_label: '获奖记录',
          },
        },
      ],
      artifacts: { selected_candidate_id: 'opaque-secret-selected-token' },
    },
    {
      key: 'FINISHED',
      label: '查询结束',
      group: 'query',
      status: 'not_run',
      summary: '前序已终止',
      events: [],
      artifacts: {},
    },
  ],
};

describe('QueryDiagnosticDialog', () => {
  it('renders context before the query stages and keeps every server artifact expandable', () => {
    const html = renderToStaticMarkup(
      createElement(QueryDiagnosticDialog, {
        open: true,
        report: REPORT,
        loading: false,
        error: null,
        onClose: vi.fn(),
        onRetry: vi.fn(),
        onDownload: vi.fn(),
      }),
    );

    expect(html.indexOf('建模上下文（非本次查询）')).toBeLessThan(
      html.indexOf('本次查询'),
    );
    expect(html.indexOf('数据源与 AI 建模')).toBeLessThan(
      html.indexOf('Revision / Release 冻结版本'),
    );
    expect(html.indexOf('PRECHECK · 请求预检')).toBeLessThan(
      html.indexOf('CANDIDATE_DISCOVERY · 候选发现'),
    );
    expect(html.indexOf('CANDIDATE_DISCOVERY · 候选发现')).toBeLessThan(
      html.indexOf('FINISHED · 查询结束'),
    );
    expect(html).toContain('6 个模型，5 个分析主题');
    expect(html).toContain('ROUTE_BINDING · routing');
    expect(html).toContain('查看事件与产物');
    expect(html).toContain('proposal_count');
    expect(html).toContain('获奖记录');
    expect(html).toContain('待确认');
    expect(html).toContain('未运行');
    expect(html).not.toContain('opaque-secret-candidate-token');
    expect(html).not.toContain('opaque-secret-selected-token');
  });

  it('keeps an export failure inside the dialog and offers an explicit retry', () => {
    const html = renderToStaticMarkup(
      createElement(QueryDiagnosticDialog, {
        open: true,
        report: null,
        loading: false,
        error: '诊断记录已过期',
        onClose: vi.fn(),
        onRetry: vi.fn(),
        onDownload: vi.fn(),
      }),
    );

    expect(html).toContain('原问数结果不受影响');
    expect(html).toContain('诊断记录已过期');
    expect(html).toContain('重试');
  });

  it('builds a Markdown Blob from the exact server report', async () => {
    const blob = createDiagnosticMarkdownBlob(REPORT);

    expect(blob.type).toBe('text/markdown; charset=utf-8');
    expect(await blob.text()).toBe(REPORT.markdown);
  });

  it('allows only one export per flight and reopens the gate after failure for retry', async () => {
    let rejectFirst!: (error: Error) => void;
    const firstExport = new Promise<AnalyticsQueryDiagnosticExport>((_, reject) => {
      rejectFirst = reject;
    });
    const exportTask = vi
      .fn<() => Promise<AnalyticsQueryDiagnosticExport>>()
      .mockReturnValueOnce(firstExport)
      .mockResolvedValueOnce(REPORT);
    const inFlight = { current: false };

    const first = runDiagnosticExportOnce(inFlight, exportTask);
    const duplicate = runDiagnosticExportOnce(inFlight, exportTask);

    expect(exportTask).toHaveBeenCalledTimes(1);
    await expect(duplicate).resolves.toBeUndefined();
    rejectFirst(new Error('temporary failure'));
    await expect(first).rejects.toThrow('temporary failure');
    expect(inFlight.current).toBe(false);

    await expect(runDiagnosticExportOnce(inFlight, exportTask)).resolves.toBe(REPORT);
    expect(exportTask).toHaveBeenCalledTimes(2);
  });

  it('downloads the exact Blob with the server filename and releases the object URL', async () => {
    let downloadedBlob: Blob | undefined;
    const environment = {
      createObjectUrl: vi.fn((blob: Blob) => {
        downloadedBlob = blob;
        return 'blob:diagnostic-report';
      }),
      clickDownload: vi.fn(),
      defer: vi.fn((callback: () => void) => callback()),
      revokeObjectUrl: vi.fn(),
    };

    downloadDiagnosticMarkdown(REPORT, environment);

    expect(environment.clickDownload).toHaveBeenCalledWith(
      'blob:diagnostic-report',
      'knowflow-diagnostic-query-1.md',
    );
    expect(environment.revokeObjectUrl).toHaveBeenCalledWith('blob:diagnostic-report');
    expect(downloadedBlob?.type).toBe('text/markdown; charset=utf-8');
    expect(await downloadedBlob?.text()).toBe(REPORT.markdown);
  });
});
