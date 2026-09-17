import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { AnalyticsRevision } from '@analytics/api/types';
import { FieldEditor } from './entity-editor';

const revision = {
  id: 'revision-1',
  etag: 7,
  schema_snapshot_hash: 'sha256:schema',
} as unknown as AnalyticsRevision;

const render = (props: Parameters<typeof FieldEditor>[0]) =>
  renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
      createElement(FieldEditor, props),
    ),
  );

describe('FieldEditor', () => {
  it('does not expose a raw-field description as if it affected online querying', () => {
    const html = render({
      projectId: 'proj',
      revision,
      modelTableName: 'score_model_acct',
      field: {
        id: 'field-platform-id',
        model_id: 'model-platform',
        name: '电商平台 ID',
        column: '电商平台id',
        data_type: 'text',
        kind: 'identifier',
        identifier_type: 'foreign',
        dimension_type: null,
        semantic_expr: '"电商平台id"',
        unit: null,
        default_aggregation: null,
        description: '不会进入在线问数',
        aliases: [],
        nullable: false,
        create_dimension: true,
        create_metric: false,
      },
      blockers: [],
      relations: [],
      onJumpMetric: vi.fn(),
      saving: false,
      onClose: vi.fn(),
      onSave: vi.fn(),
    });

    expect(html).toContain('业务名称');
    expect(html).not.toContain('<textarea');
    expect(html).not.toContain('会进入模型提示');
  });

  it('把「用数据核对」摆在标识类型旁边，就在选错主标识的那个位置', () => {
    const html = render({
      projectId: 'proj',
      revision,
      modelTableName: 'score_model_acct',
      field: {
        id: 'score_model_acct.zhhao',
        model_id: 'score_model_acct',
        name: '账号',
        column: 'zhhao',
        data_type: 'varchar(64)',
        kind: 'identifier',
        identifier_type: 'primary',
        dimension_type: null,
        semantic_expr: '"zhhao"',
        unit: null,
        default_aggregation: null,
        description: '',
        aliases: [],
        nullable: false,
        create_dimension: false,
        create_metric: false,
      },
      blockers: [],
      relations: [],
      onJumpMetric: vi.fn(),
      saving: false,
      onClose: vi.fn(),
      onSave: vi.fn(),
    });

    expect(html).toContain('标识类型');
    expect(html).toContain('用数据核对');
    expect(html).toContain('非空且唯一');
  });
});
