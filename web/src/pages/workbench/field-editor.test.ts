import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { FieldEditor } from './entity-editor';

describe('FieldEditor', () => {
  it('does not expose a raw-field description as if it affected online querying', () => {
    const html = renderToStaticMarkup(
      createElement(FieldEditor, {
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
      }),
    );

    expect(html).toContain('业务名称');
    expect(html).not.toContain('<textarea');
    expect(html).not.toContain('会进入模型提示');
  });
});
