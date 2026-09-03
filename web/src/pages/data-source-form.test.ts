import { describe, expect, it } from 'vitest';
import {
  ENGINES,
  canCreateProject,
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

  it('还没选就什么都不做', () => {
    // 打开对话框但没选，点保存不该往服务端发一次空的写。
    expect(dataSourceBindingAction({ boundId: null, selected: '' })).toBe('none');
  });

  it('换成另一个数据源是绑定', () => {
    expect(dataSourceBindingAction({ boundId: 'ds_1', selected: 'ds_2' })).toBe('bind');
  });

  it('第一次绑上去也是绑定', () => {
    expect(dataSourceBindingAction({ boundId: null, selected: 'ds_1' })).toBe('bind');
  });

  it('没有解绑这个动作', () => {
    /**
     * 项目必须一直有数据源。部署配置的那个库在启动迁移里已经变成一个普通数据源
     * 记录，"回到默认库"就是选中那一条，跟选别的没区别——所以只剩 none 和 bind。
     */
    const actions = new Set([
      dataSourceBindingAction({ boundId: 'ds_1', selected: 'ds_1' }),
      dataSourceBindingAction({ boundId: 'ds_1', selected: 'ds_2' }),
      dataSourceBindingAction({ boundId: null, selected: 'ds_1' }),
    ]);

    expect(actions).toEqual(new Set(['none', 'bind']));
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

  it('还没选时不提醒', () => {
    // 对话框刚打开、下拉停在"请选择"，那不是换库。
    expect(warnsAboutSemanticDrift({ boundId: 'ds_1', selected: '' })).toBe(false);
  });
});

describe('新建项目能否提交', () => {
  it('必须明确选一个数据源', () => {
    /**
     * 不强制的话，用户建完项目直接去导入表，看到一堆表却不知道这是哪个库——
     * 而建好模型之后再换库是破坏性的，所以这个选择实际上是一次性的。
     */
    expect(canCreateProject({ name: '经营分析', dataSourceChoice: '' })).toBe(false);
    expect(canCreateProject({ name: '经营分析', dataSourceChoice: 'ds_1' })).toBe(true);
  });

  it('没有「默认库」这个魔法选项', () => {
    /**
     * 曾经有过：不选就静默回落到部署配置的库，用户不知道自己连的是哪个。现在
     * 那个库在启动迁移里变成了一个普通数据源记录，出现在列表里，跟别的一样选。
     */
    expect(canCreateProject({ name: '经营分析', dataSourceChoice: 'ds_1' })).toBe(true);
    expect(canCreateProject({ name: '经营分析', dataSourceChoice: '' })).toBe(false);
  });

  it('名字仍然必填', () => {
    expect(canCreateProject({ name: '', dataSourceChoice: 'ds_1' })).toBe(false);
    expect(canCreateProject({ name: '   ', dataSourceChoice: 'ds_1' })).toBe(false);
  });

  it('开源独立版不受这条约束', () => {
    // 那边没有数据源这个概念，只有设置页里那一个库。
    expect(canCreateProject({ name: '经营分析' })).toBe(true);
  });
});

describe('新建项目时该不该发绑定请求', () => {
  it('选了具体数据源就绑', () => {
    expect(dataSourceBindingAction({ boundId: null, selected: 'ds_1' })).toBe('bind');
  });

  it('新项目一定要绑：创建时已经强制选过了', () => {
    // 创建对话框不允许"不选"，所以走到这里 selected 必然是一个真实数据源。
    expect(dataSourceBindingAction({ boundId: null, selected: 'ds_1' })).toBe('bind');
  });
});
