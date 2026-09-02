import { useQuery } from '@tanstack/react-query';
import { Suspense, lazy } from 'react';
import { getOssStatus } from './api/oss';
import { EDITION } from './api/client';
import { Spinner } from './components/ui';
import { useRoutes } from '@analytics/lib/router';
import { ProjectsPage } from './pages/projects';

// 项目列表是落地页，直接静态引入。其余三个懒加载。
//
// 此前四个页面全是静态导入，于是打开项目列表要先把整个建模工作台下载编译完——
// pages/workbench 一个目录 40 个文件、6700 行，还带关系画布的图形库。实测（开发
// 模式）：导航到发出第一个请求隔了 2199ms，其中最大的 vendor chunk 就占 536ms；
// 而请求本身只要 783ms。等待的大头在"还没开始请求"。
const WorkbenchPage = lazy(() =>
  import('./pages/workbench').then((m) => ({ default: m.WorkbenchPage })),
);
const AskPage = lazy(() => import('./pages/ask').then((m) => ({ default: m.AskPage })));
const SettingsPage = lazy(() =>
  import('./pages/settings').then((m) => ({ default: m.SettingsPage })),
);

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
  // 懒加载的页面各自包一层 Suspense：整表包一层的话，进入工作台时连项目列表
  // 一起被替换成 Spinner，返回时又要重挂一次。
  const lazily = (node: JSX.Element) => (
    <Suspense fallback={<Spinner />}>{node}</Suspense>
  );
  const element = useRoutes([
    { path: '/', element: <ProjectsPage ready={ready} /> },
    ...(EDITION !== 'embedded'
      ? [{ path: '/settings', element: lazily(<SettingsPage />) }]
      : []),
    { path: '/projects/:projectId', element: lazily(<WorkbenchPage />) },
    { path: '/projects/:projectId/ask', element: lazily(<AskPage />) },
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
