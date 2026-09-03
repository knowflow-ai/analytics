/**
 * 数据源表单与绑定的判断逻辑。
 *
 * 抽成纯函数是因为这几条判断错了都**不报错**，只是行为悄悄变样：保存按钮该不该
 * 亮、点了保存到底是绑还是解绑、换库该不该提醒。本仓的测试都是纯逻辑（没有 DOM
 * 测试设施），所以判断必须住在组件外面才测得到。
 */

/** 与后端 SqlDialect 一一对应。多一个少一个都会在保存时被服务端拒绝。 */
export const ENGINES = [
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
] as const;

const DSN_PLACEHOLDER: Record<string, string> = {
  postgres: 'postgresql+psycopg://用户:密码@主机:5432/库名',
  mysql: 'mysql+pymysql://用户:密码@主机:3306/库名?charset=utf8mb4',
};

export function engineLabel(engine: string): string {
  return ENGINES.find((item) => item.value === engine)?.label ?? engine;
}

export function dsnPlaceholder(engine: string): string {
  return DSN_PLACEHOLDER[engine] ?? '';
}

/**
 * 表单能不能保存。
 *
 * 新建必须给连接串；修改可以只改名字——连接串取不回来，留空表示不动它。
 */
export function canSaveDataSource(input: {
  name: string;
  dsn: string;
  editing: boolean;
}): boolean {
  if (input.name.trim().length === 0) return false;
  return input.editing || input.dsn.trim().length > 0;
}

/**
 * 保存绑定时该做什么。
 *
 * 「没变化」要单独识别出来：不识别的话，用户点开只想看一眼、直接点保存，就会往
 * 服务端发一次多余的写。
 *
 * 没有「解绑」这个动作：项目必须一直有数据源。部署配置的那个库在启动迁移里已经
 * 变成了一个普通数据源记录，所以"回到默认库"就是选中那一条，跟选别的没有区别。
 */
export function dataSourceBindingAction(input: {
  boundId: string | null;
  selected: string;
}): 'none' | 'bind' {
  // 空选择 = 还没选（下拉停在"请选择"），不是"要换到某个库"。不单独判的话，
  // 打开对话框就会被当成换库：保存按钮亮着、还弹一条换库警告。
  if (!input.selected) return 'none';
  return input.selected === input.boundId ? 'none' : 'bind';
}

/**
 * 换库要不要提醒。
 *
 * 只在**已经绑着别的库**时提醒：语义模型是按原来那个库的表结构建的，换过去表
 * 未必存在、列类型未必一样。从"默认库"第一次绑上去不算换——那是初次配置。
 */
export function warnsAboutSemanticDrift(input: {
  boundId: string | null;
  selected: string;
}): boolean {
  if (input.boundId === null) return false;
  return dataSourceBindingAction(input) === 'bind';
}


/**
 * 新建项目能不能提交。
 *
 * 数据源必须明确选一个。不强制的话，用户建完项目直接去导入表，看到一堆表却不知道
 * 这是哪个库；而建好模型之后再换库是破坏性的（表未必存在、列类型未必一样），所以
 * 这个选择实际上是一次性的，就该在创建时当面做。
 *
 * 不再有「默认库（部署配置）」这个魔法选项：部署配置的那个库在启动迁移里已经变成
 * 一个普通数据源记录，跟别的一样出现在列表里。
 *
 * 开源独立版没有数据源这个概念（只有设置页里那一个库），传 undefined 跳过这条。
 */
export function canCreateProject(input: {
  name: string;
  dataSourceChoice?: string;
}): boolean {
  if (input.name.trim().length === 0) return false;
  if (input.dataSourceChoice === undefined) return true;
  return input.dataSourceChoice.length > 0;
}
