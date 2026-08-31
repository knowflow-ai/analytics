/**
 * 部署形态判定:同一份源码要被 Vite 和 webpack(umi)分别编译。
 *
 * 不能用 import.meta.env —— 那是 Vite 语法,webpack 编译时直接报错。
 * 判定靠运行时事实:页面挂在宿主的 /analytics 路径下,就是嵌入商业版;
 * 独立部署时 SPA 占据整个站点根路径。
 *
 * - oss:直连核心路径,Bearer token 走自己的登录。
 * - embedded:商业版门卫直通道(/v1/analytics/core),复用宿主 localStorage
 *   的 Authorization;401 交给宿主登录页,不弹自己的。
 */
export const HOST_BASENAME = '/analytics';

export function editionForPathname(pathname: string): 'oss' | 'embedded' {
  // 必须是完整路径段:裸 startsWith 会把宿主的 /analytics-legacy(旧实现的
  // 回退路由)也判成嵌入版。
  if (pathname === HOST_BASENAME) return 'embedded';
  return pathname.startsWith(`${HOST_BASENAME}/`) ? 'embedded' : 'oss';
}

export const EDITION =
  typeof window === 'undefined' ? 'oss' : editionForPathname(window.location.pathname);
export const API_ROOT = EDITION === 'embedded' ? '/v1/analytics/core' : '';

/**
 * SPA 内部导航路径。
 *
 * 独立部署时 SPA 占据站点根,`/projects/x` 就是最终地址;嵌入商业版时它挂在
 * 宿主的 /analytics 下,同一个字符串会跳到宿主根上的 /projects/x —— 404。
 * 所有跨页导航都必须过这个函数,不要手写绝对路径。
 */
export function pathForEdition(path: string, edition: 'oss' | 'embedded'): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  if (edition !== 'embedded') return normalized;
  return normalized === '/' ? HOST_BASENAME : `${HOST_BASENAME}${normalized}`;
}

export function appPath(path: string): string {
  return pathForEdition(path, EDITION);
}
