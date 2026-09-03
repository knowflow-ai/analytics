import { describe, expect, it } from 'vitest';
import {
  ENGINES,
  FALLBACK_DATA_SOURCE,
  canSaveDataSource,
  dataSourceBindingAction,
  dsnPlaceholder,
  engineLabel,
  warnsAboutSemanticDrift,
} from './data-source-form';

describe('引擎', () => {
  it('只提供后端认识的两种', () => {
    // 多给一种，用户填完一整套连接信息才会在保存时被服务端拒绝。
    expect(ENGINES.map((item) => item.value)).toEqual(['postgres', 'mysql']);
  });

  it('展示名是数据库的正式写法', () => {
    expect(engineLabel('postgres')).toBe('PostgreSQL');
    expect(engineLabel('mysql')).toBe('MySQL');
  });

  it('认不出的引擎原样显示，不显示空白', () => {
    // 后端将来加了新引擎而前端没跟上时，显示 'oracle' 也比显示空白强。
    expect(engineLabel('oracle')).toBe('oracle');
  });

  it('两种引擎各有自己的连接串示例', () => {
    expect(dsnPlaceholder('postgres')).toContain('postgresql+psycopg://');
    // 驱动名写错连不上，示例得给对：MySQL 用 pymysql，且要带 charset。
    expect(dsnPlaceholder('mysql')).toContain('mysql+pymysql://');
    expect(dsnPlaceholder('mysql')).toContain('charset=utf8mb4');
  });
});

describe('表单能否保存', () => {
  it('新建要有名字和连接串', () => {
    expect(canSaveDataSource({ name: '生产库', dsn: 'x', editing: false })).toBe(true);
    expect(canSaveDataSource({ name: '生产库', dsn: '', editing: false })).toBe(false);
    expect(canSaveDataSource({ name: '', dsn: 'x', editing: false })).toBe(false);
  });

  it('修改可以只改名字', () => {
    // 连接串取不回来，留空表示不动它——不允许的话就没法只改个名字。
    expect(canSaveDataSource({ name: '新名', dsn: '', editing: true })).toBe(true);
  });

  it('只有空白的名字不算名字', () => {
    expect(canSaveDataSource({ name: '   ', dsn: 'x', editing: false })).toBe(false);
  });
});

describe('绑定动作', () => {
  it('选的就是当前绑的，什么都不做', () => {
    expect(dataSourceBindingAction({ boundId: 'ds_1', selected: 'ds_1' })).toBe('none');
  });

  it('未绑定时选默认库，也什么都不做', () => {
    // 这是**多数项目的常态**（存量项目一个绑定行都没有），不能每次点保存都发一次解绑。
    expect(
      dataSourceBindingAction({ boundId: null, selected: FALLBACK_DATA_SOURCE }),
    ).toBe('none');
  });

  it('换成另一个数据源是绑定', () => {
    expect(dataSourceBindingAction({ boundId: 'ds_1', selected: 'ds_2' })).toBe('bind');
  });

  it('第一次绑上去也是绑定', () => {
    expect(dataSourceBindingAction({ boundId: null, selected: 'ds_1' })).toBe('bind');
  });

  it('选回默认库是解绑', () => {
    expect(
      dataSourceBindingAction({ boundId: 'ds_1', selected: FALLBACK_DATA_SOURCE }),
    ).toBe('unbind');
  });
});

describe('换库提醒', () => {
  it('已经绑着别的库时提醒', () => {
    // 语义模型按原库的表结构建的，换过去表未必存在、列类型未必一样。
    expect(warnsAboutSemanticDrift({ boundId: 'ds_1', selected: 'ds_2' })).toBe(true);
  });

  it('从默认库第一次绑上去不提醒', () => {
    // 那是初次配置，不是换库；每次都弹提醒会让人忽略它。
    expect(warnsAboutSemanticDrift({ boundId: null, selected: 'ds_1' })).toBe(false);
  });

  it('没有变化不提醒', () => {
    expect(warnsAboutSemanticDrift({ boundId: 'ds_1', selected: 'ds_1' })).toBe(false);
  });

  it('解绑回默认库也提醒', () => {
    // 一样是换库：默认库和原来那个库是两个库。
    expect(
      warnsAboutSemanticDrift({ boundId: 'ds_1', selected: FALLBACK_DATA_SOURCE }),
    ).toBe(true);
  });
});
