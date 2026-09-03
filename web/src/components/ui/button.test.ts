import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { Button, ToastProvider, useToast } from './index';

const render = (props: Record<string, unknown>, children?: string) =>
  renderToStaticMarkup(createElement(Button, props, children));

describe('Button', () => {
  it('标签永远不换行', () => {
    /**
     * 真实故障：数据源对话框头部是 `flex justify-between`，旁边一段说明文字把
     * 按钮挤窄，「新建」裂成两行并从 h-9 的固定高度里溢出，按钮整个变形。
     *
     * 挤不下时该让位的是文字，不是按钮。
     */
    const html = render({}, '新建');

    expect(html).toContain('whitespace-nowrap');
    expect(html).toContain('shrink-0');
  });

  it('高度固定，所以换行会溢出而不是撑高', () => {
    // 这条解释了上一条为什么必须成立：h-9 拦不住换行，只会让文字跑出边框。
    expect(render({}, 'x')).toContain('h-9');
  });

  it('图标与文字之间有间距', () => {
    expect(render({}, 'x')).toContain('gap-1.5');
  });

  it('小号按钮也不换行', () => {
    expect(render({ size: 'sm' }, '新建')).toContain('whitespace-nowrap');
  });

  it('调用方的 className 追加在后面，能覆盖默认值', () => {
    // 放在前面会被默认类压过去，调用方就没法微调了。
    const html = render({ className: 'w-full' }, 'x');

    expect(html.indexOf('w-full')).toBeGreaterThan(html.indexOf('whitespace-nowrap'));
  });

  it('loading 时禁用并显示转圈', () => {
    const html = render({ loading: true }, '保存');

    expect(html).toContain('disabled');
    expect(html).toContain('animate-spin');
  });
});

describe('Toast 默认值', () => {
  it('没有 Provider 时会出声，而不是静默丢弃', () => {
    /**
     * 真实故障：宿主页面直接渲染 SPA 却漏挂 ToastProvider，整个嵌入版的提示全部
     * 消失——不报错、不警告，表现为「点了保存没有任何反应」而请求其实成功了。
     *
     * 一个静默的空默认值，让"忘了挂"变成了查不出来的故障。
     */
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const Probe = () => {
      useToast().success('存好了');
      return null;
    };

    renderToStaticMarkup(createElement(Probe));

    expect(warn).toHaveBeenCalled();
    expect(String(warn.mock.calls[0][0])).toContain('存好了');
    warn.mockRestore();
  });

  it('挂了 Provider 就不再警告', () => {
    // 这条是上一条的对照：没有它，上一条可能只是"warn 总会被调"而已。
    // ToastProvider 用 window.setTimeout 做自动消失，node 环境里没有 window。
    vi.stubGlobal('window', { setTimeout: () => 0 });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const Probe = () => {
      useToast().success('存好了');
      return null;
    };

    renderToStaticMarkup(
      createElement(ToastProvider, null, createElement(Probe)),
    );

    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
    vi.unstubAllGlobals();
  });
});
