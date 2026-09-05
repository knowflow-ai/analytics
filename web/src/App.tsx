import { useQuery } from '@tanstack/react-query';
import { BarChart3, Settings } from 'lucide-react';
import { AnalyticsRoutes } from './AnalyticsRoutes';
import { NavLink } from '@analytics/lib/router';
import { getOssStatus } from './api/oss';
import { EDITION, getAccessToken, UNAUTHORIZED_EVENT } from './api/client';
import { useEffect, useState } from 'react';
import { Spinner } from './components/ui';
import { LoginPage } from './pages/login';
import { ANALYTICS_MAX_CONTENT_WIDTH_PX } from './lib/layout';

function TopNav() {
  const link = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] transition-colors ${
      isActive ? 'bg-slate-100 font-medium text-slate-900' : 'text-slate-500 hover:text-slate-800'
    }`;
  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/90 backdrop-blur">
      <div
        className="mx-auto flex h-12 w-full items-center gap-6 px-5"
        style={{ maxWidth: ANALYTICS_MAX_CONTENT_WIDTH_PX }}
      >
        <NavLink to="/" className="flex items-center gap-2 text-sm font-semibold text-slate-900">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-gradient-to-br from-blue-600 to-blue-400 text-white">
            <BarChart3 className="h-4 w-4" />
          </span>
          KnowFlow 智能问数
        </NavLink>
        <nav className="flex items-center gap-1">
          <NavLink to="/" end className={link}>
            数据源
          </NavLink>
          {EDITION !== 'embedded' && (
            <NavLink to="/settings" className={link}>
              <Settings className="h-3.5 w-3.5" />
              设置
            </NavLink>
          )}
        </nav>

      </div>
    </header>
  );
}

export function App() {
  const status = useQuery({ queryKey: ['oss-status'], queryFn: getOssStatus });
  const [authVersion, setAuthVersion] = useState(0);
  useEffect(() => {
    const bump = () => setAuthVersion((v) => v + 1);
    window.addEventListener(UNAUTHORIZED_EVENT, bump);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, bump);
  }, []);
  if (status.isPending) return <Spinner />;
  if (status.isError) {
    return (
      <div className="p-10 text-center text-sm text-red-600">
        无法连接服务：{String(status.error)}
      </div>
    );
  }
  if (status.data.login_required && !getAccessToken()) {
    return <LoginPage key={authVersion} onLoggedIn={() => setAuthVersion((v) => v + 1)} />;
  }
  // 开源版外壳:自有顶栏 + 登录门。内容本体与商业版共用 AnalyticsRoutes。
  return (
    <div className="flex min-h-full flex-col">
      <TopNav />
      <main
        className="mx-auto w-full flex-1 px-5 py-6"
        style={{ maxWidth: ANALYTICS_MAX_CONTENT_WIDTH_PX }}
      >
        <AnalyticsRoutes />
      </main>
    </div>
  );
}
