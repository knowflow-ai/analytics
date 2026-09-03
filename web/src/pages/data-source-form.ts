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

/** 未绑定的哨兵值。项目不绑数据源时回落到部署配置的默认库。 */
export const FALLBACK_DATA_SOURCE = '__fallback__';

/**
 * 保存绑定时该做什么。
 *
 * 「没变化」要单独识别出来：不识别的话，用户点开只想看一眼、直接点保存，就会往
 * 服务端发一次多余的写；而如果回填又没做对，那一次写会把项目从"绑着 A"改成
 * "未绑定"——未绑定会静默回落到默认库，数字看起来完全正常。
 */
export function dataSourceBindingAction(input: {
  boundId: string | null;
  selected: string;
}): 'none' | 'bind' | 'unbind' {
  const current = input.boundId ?? FALLBACK_DATA_SOURCE;
  if (input.selected === current) return 'none';
  return input.selected === FALLBACK_DATA_SOURCE ? 'unbind' : 'bind';
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
  return dataSourceBindingAction(input) !== 'none';
}
