/**
 * 上传表格的判断逻辑。
 *
 * 抽成纯函数是因为这几条错了都**不报错**，只是行为悄悄变样：能不能提交、提交的是新建
 * 还是灌数、表名默认叫什么。本仓的测试都是纯逻辑（没有 DOM 测试设施），判断住在组件
 * 外面才测得到。
 */

/** 只收 .xlsx。.xls 要另一颗解析依赖，核心侧没装。 */
export const ACCEPTED_SUFFIX = '.xlsx';

export function isAcceptedFile(name: string): boolean {
  return name.trim().toLowerCase().endsWith(ACCEPTED_SUFFIX);
}

/**
 * 表名的默认值：文件名去掉后缀。
 *
 * 用户十有八九就想叫这个名字，让他每次重打一遍是白费事；但仍然可改——建模页和问数里
 * 他要靠这个名字找到这张表。
 */
export function defaultTableName(fileName: string): string {
  const base = fileName.replace(/\.[^.]+$/, '').trim();
  return base;
}

/** 表名能不能用。PostgreSQL 标识符上限 63 字节，这里按字符数保守判。 */
export function tableNameProblem(name: string, existing: readonly string[]): string {
  const value = name.trim();
  if (!value) return '请给这张表起个名字。';
  if (value.length > 63) return '表名太长了，请控制在 63 个字符以内。';
  if (existing.some((item) => item === value)) {
    return `已经有一张叫「${value}」的表了。换个名字，或到列表里删掉那张。`;
  }
  return '';
}

/** 能不能提交新建。 */
export function canCreate(input: {
  file: File | null;
  sheet: string;
  table: string;
  existing: readonly string[];
}): boolean {
  return (
    input.file !== null &&
    input.sheet !== '' &&
    tableNameProblem(input.table, input.existing) === ''
  );
}

/** 灌数时选的是哪张已有的表。新建与灌数共用一个弹窗，模式错了会把数据写错地方。 */
export type UploadMode = 'create' | 'append' | 'replace';

export function modeLabel(mode: UploadMode): string {
  if (mode === 'create') return '新建一张表';
  if (mode === 'append') return '追加到已有的表';
  return '替换已有表的全部数据';
}

export function canLoad(input: {
  file: File | null;
  sheet: string;
  table: string;
}): boolean {
  return input.file !== null && input.sheet !== '' && input.table !== '';
}
