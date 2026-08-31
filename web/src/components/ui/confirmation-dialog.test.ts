import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmationDialog, wrappedDialogFocusIndex } from './index';

describe('ConfirmationDialog', () => {
  it('renders a product-styled destructive confirmation with explicit actions', () => {
    const html = renderToStaticMarkup(
      createElement(
        ConfirmationDialog,
        {
          open: true,
          title: '确认删除“电商平台 ID”',
          description: '删除后将同步处理以下影响。',
          confirmText: '确认删除',
          danger: true,
          loading: false,
          onConfirm: vi.fn(),
          onClose: vi.fn(),
        },
        createElement('div', null, '删除 1 条语义上下文'),
      ),
    );

    expect(html).toContain('role="alertdialog"');
    expect(html).toContain('aria-labelledby=');
    expect(html).toContain('tabindex="-1"');
    expect(html).toContain('autofocus=""');
    expect(html).toContain('确认删除“电商平台 ID”');
    expect(html).toContain('删除后将同步处理以下影响。');
    expect(html).toContain('删除 1 条语义上下文');
    expect(html).toContain('确认删除');
    expect(html).toContain('取消');
    expect(html).toContain('bg-red-600');
  });

  it('wraps keyboard focus only at the dialog boundaries', () => {
    expect(wrappedDialogFocusIndex(0, 3, true)).toBe(2);
    expect(wrappedDialogFocusIndex(2, 3, false)).toBe(0);
    expect(wrappedDialogFocusIndex(1, 3, false)).toBeNull();
    expect(wrappedDialogFocusIndex(-1, 3, false)).toBe(0);
    expect(wrappedDialogFocusIndex(-1, 3, true)).toBe(2);
    expect(wrappedDialogFocusIndex(-1, 0, false)).toBe(-1);
  });

  it('does not render while closed', () => {
    const html = renderToStaticMarkup(
      createElement(ConfirmationDialog, {
        open: false,
        title: '确认删除',
        onConfirm: vi.fn(),
        onClose: vi.fn(),
      }),
    );

    expect(html).toBe('');
  });
});
