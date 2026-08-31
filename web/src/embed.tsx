import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import { ToastProvider } from './components/ui';
import './index.css';

/**
 * 微前端挂载入口:宿主(ragflow web)动态加载 embed.js,把本应用挂进一个
 * Shadow Root。样式双向隔离——tailwind 的全局 reset 关在影子里,宿主的
 * antd 不受影响;同一个页面文档,滚动、地址栏、前进后退全部原生。
 *
 * basename 由宿主传入(商业版为 /analytics),SPA 子路由直接写进宿主
 * 地址栏,深链接可刷新可分享。
 */
export interface EmbedHandle {
  unmount(): void;
}

export function mount(host: HTMLElement, options?: { basename?: string }): EmbedHandle {
  const shadow = host.shadowRoot ?? host.attachShadow({ mode: 'open' });
  shadow.innerHTML = '';
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = new URL('./embed.css', import.meta.url).toString();
  shadow.appendChild(stylesheet);
  const container = document.createElement('div');
  container.className = 'kf-embed-root';
  shadow.appendChild(container);

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const root = ReactDOM.createRoot(container);
  root.render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <BrowserRouter basename={options?.basename ?? '/'}>
            <App />
          </BrowserRouter>
        </ToastProvider>
      </QueryClientProvider>
    </React.StrictMode>,
  );
  return {
    unmount() {
      root.unmount();
    },
  };
}
