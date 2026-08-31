import { useQuery } from '@tanstack/react-query';
import { getOssStatus } from './api/oss';
import { EDITION } from './api/client';
import { Spinner } from './components/ui';
import { useRoutes } from '@analytics/lib/router';
import { AskPage } from './pages/ask';
import { ProjectsPage } from './pages/projects';
import { SettingsPage } from './pages/settings';
import { WorkbenchPage } from './pages/workbench';

/**
 * 页面内容本体:只有路由表,不含 Router、登录门和外壳。
 *
 * 两个构建体系共用同一份源码——开源版由 App 套上自己的顶栏与 BrowserRouter,
 * 商业版由宿主 umi 提供 Router、侧边栏与面包屑,直接渲染本组件。路由表用
 * useRoutes 而不是 <Routes>:umi 没有导出 Routes/Route 组件。
 */
export function AnalyticsRoutes() {
  // 嵌入模式没有 OSS 外壳:数据源与模型由宿主(租户配置)负责。
  const status = useQuery({
    queryKey: ['oss-status'],
    queryFn:
      EDITION === 'embedded'
        ? async () => ({ ready: true, login_required: false })
        : getOssStatus,
  });
  const ready = status.data?.ready ?? false;
  const element = useRoutes([
    { path: '/', element: <ProjectsPage ready={ready} /> },
    ...(EDITION !== 'embedded' ? [{ path: '/settings', element: <SettingsPage /> }] : []),
    { path: '/projects/:projectId', element: <WorkbenchPage /> },
    { path: '/projects/:projectId/ask', element: <AskPage /> },
    { path: '*', element: <ProjectsPage ready={ready} /> },
  ]);
  if (status.isPending) return <Spinner />;
  if (status.isError) {
    return (
      <div className="p-10 text-center text-sm text-red-600">
        无法连接服务：{String(status.error)}
      </div>
    );
  }
  return element;
}
