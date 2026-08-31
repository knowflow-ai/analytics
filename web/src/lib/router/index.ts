/**
 * 路由适配层:同一份页面源码要被两个构建体系编译。
 *
 * - 开源版(Vite)直接用 react-router-dom。
 * - 商业版(umi)必须用 umi 自己导出的路由 API:umi 内嵌的 react-router
 *   是另一个实例,直接 import react-router-dom 会拿到不共享的 context,
 *   Link/useNavigate 当场抛 "may be used only in the context of a Router"。
 *
 * 构建时由各自的 alias 把这个模块指向对应实现。umi 没有导出 Routes/Route
 * 组件,所以路由表统一用两边都支持的 useRoutes()。
 */
export * from './react-router';
