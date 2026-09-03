import { beforeEach, describe, expect, it, vi } from 'vitest';

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock('./client', () => ({
  EDITION: 'oss',
  request: requestMock,
}));

import {
  bindProjectDataSource,
  createDataSource,
  deleteDataSource,
  getProjectDataSource,
  listDataSources,
  testDataSource,
  unbindProjectDataSource,
  updateDataSource,
} from './analytics';

beforeEach(() => {
  requestMock.mockReset();
  requestMock.mockResolvedValue({});
});

const path = () => String(requestMock.mock.calls[0][0]);
const options = () => (requestMock.mock.calls[0][1] ?? {}) as Record<string, unknown>;

describe('数据源接口的路径', () => {
  it('全部写成核心路径，由 client 决定去哪', async () => {
    /**
     * 嵌入版的 rewritePath 把 `/v1/analytics/...` 转成 `/v1/analytics/core/...`。
     * 在这里写死 `/core` 会被再重写一次变成 `core/core`——**静默 404**，前端只看到
     * 一个空列表，没有任何报错。
     */
    await listDataSources();

    expect(path()).toBe('/v1/analytics/data-sources');
    expect(path()).not.toContain('/core');
  });

  it('连通性测试是核心的 :test 动作路径', async () => {
    await testDataSource({ engine: 'mysql', dsn: 'x' });

    expect(path()).toBe('/v1/analytics/data-sources:test');
  });

  it('单个数据源的路径带 id 且转义', async () => {
    await updateDataSource('ds/1', { name: 'x' });

    expect(path()).toBe('/v1/analytics/data-sources/ds%2F1');
  });

  it('删除打的是同一条路径', async () => {
    await deleteDataSource('ds_1');

    expect(path()).toBe('/v1/analytics/data-sources/ds_1');
    expect(options().method).toBe('DELETE');
  });
});

describe('项目绑定', () => {
  it('读取绑定带上项目头', async () => {
    // 少了 projectId，直通道拿不到项目上下文，核心会以 project scope mismatch 拒绝。
    await getProjectDataSource('prj_1');

    expect(path()).toBe('/v1/analytics/projects/prj_1/data-source');
    expect(options().projectId).toBe('prj_1');
  });

  it('绑定送出数据源 id', async () => {
    await bindProjectDataSource('prj_1', 'ds_1');

    expect(options().method).toBe('PUT');
    expect(options().body).toEqual({ data_source_id: 'ds_1' });
  });

  it('解绑是 DELETE', async () => {
    await unbindProjectDataSource('prj_1');

    expect(options().method).toBe('DELETE');
    expect(options().projectId).toBe('prj_1');
  });

  it('没绑数据源时返回 null 而不是抛错', async () => {
    // 这是**多数项目的常态**（存量项目一个绑定行都没有），不能当异常处理。
    requestMock.mockResolvedValue({ data_source: null });

    await expect(getProjectDataSource('prj_1')).resolves.toBeNull();
  });
});

describe('凭据只进不出', () => {
  it('创建时把连接串送出去', async () => {
    await createDataSource({ name: '生产库', engine: 'postgres', dsn: 'postgresql://x' });

    expect(options().body).toEqual({
      name: '生产库',
      engine: 'postgres',
      dsn: 'postgresql://x',
    });
  });

  it('列表返回值里没有装连接串的地方', async () => {
    requestMock.mockResolvedValue({
      items: [{ id: 'ds_1', name: '生产库', engine: 'postgres' }],
    });

    const items = await listDataSources();

    expect(items[0]).not.toHaveProperty('dsn');
    expect(items[0]).not.toHaveProperty('secret');
  });

  it('列表缺字段时给空数组，不是 undefined', async () => {
    requestMock.mockResolvedValue({});

    await expect(listDataSources()).resolves.toEqual([]);
  });
});
