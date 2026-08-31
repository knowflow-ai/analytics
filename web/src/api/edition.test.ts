import { describe, expect, it } from 'vitest';
import { editionForPathname, pathForEdition } from './edition';

/**
 * 真实故障:项目卡片写死 navigate('/projects/x')。独立部署时对,嵌入商业版时
 * SPA 挂在 /analytics 下,这个地址跳到宿主根上,直接 404。
 */
describe('部署形态与导航路径', () => {
  it('按路径前缀判定形态', () => {
    expect(editionForPathname('/projects/prj_1')).toBe('oss');
    expect(editionForPathname('/')).toBe('oss');
    expect(editionForPathname('/analytics')).toBe('embedded');
    expect(editionForPathname('/analytics/projects/prj_1')).toBe('embedded');
    // 前缀必须落在路径段边界:宿主的 /analytics-legacy 是旧实现的回退路由,
    // 不是本应用。
    expect(editionForPathname('/analytics-legacy')).toBe('oss');
    expect(editionForPathname('/analyticsfoo')).toBe('oss');
  });

  it('独立部署时路径原样', () => {
    expect(pathForEdition('/projects/prj_1', 'oss')).toBe('/projects/prj_1');
    expect(pathForEdition('/', 'oss')).toBe('/');
  });

  it('嵌入时补上宿主前缀', () => {
    expect(pathForEdition('/projects/prj_1', 'embedded')).toBe('/analytics/projects/prj_1');
    expect(pathForEdition('/settings', 'embedded')).toBe('/analytics/settings');
    expect(pathForEdition('/', 'embedded')).toBe('/analytics');
  });
});
