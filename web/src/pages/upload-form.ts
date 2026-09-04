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

/** 一次最多导入多少张。与核心侧的上限一致；超了核心会拒，但在这里就说清楚更好。 */
export const MAX_SHEETS_PER_IMPORT = 20;

/** 一张待导入的表：勾没勾、叫什么、这张能不能导。 */
export interface SheetPlanRow {
  sheet: string;
  table: string;
  selected: boolean;
  /** 这张 sheet 读不出来（空表、只有表头）时的原因。有值就不能勾。 */
  blocked?: string;
}

/**
 * 多 sheet 时表名默认取 sheet 名；只有一张时取文件名。
 *
 * 一张的时候用户想的是"导入这个文件"，多张的时候想的是"导入这几张表"——默认值跟着
 * 他脑子里的那个名字走，比一律用文件名少改几次。
 */
export function defaultTableNames(fileName: string, sheets: string[]): Record<string, string> {
  const single = sheets.length === 1;
  return Object.fromEntries(
    sheets.map((sheet) => [sheet, single ? defaultTableName(fileName) : sheet]),
  );
}

/** 这一批里哪些表名有问题。返回 sheet → 问题描述，没问题的不出现。 */
export function planProblems(
  rows: readonly SheetPlanRow[],
  existing: readonly string[],
): Record<string, string> {
  const chosen = rows.filter((row) => row.selected && !row.blocked);
  const problems: Record<string, string> = {};
  for (const row of chosen) {
    const name = row.table.trim();
    const clash = chosen.filter((other) => other.table.trim() === name).length > 1;
    // 同一批里两张用同一个名字：先建的会让后建的撞上"已存在"，报出来的原因和
    // 真正的错因（这一批自己重复了）对不上。
    if (clash) {
      problems[row.sheet] = '这一批里有另一张表也叫这个名字。';
      continue;
    }
    const problem = tableNameProblem(name, existing);
    if (problem) problems[row.sheet] = problem;
  }
  return problems;
}

/** 能不能提交这一批。 */
export function canImportPlan(
  rows: readonly SheetPlanRow[],
  existing: readonly string[],
): boolean {
  const chosen = rows.filter((row) => row.selected && !row.blocked);
  if (chosen.length === 0 || chosen.length > MAX_SHEETS_PER_IMPORT) return false;
  return Object.keys(planProblems(rows, existing)).length === 0;
}

/** 把结果说成人话：几张成功、几张没有。 */
export function summarizeOutcomes(
  results: readonly { table: string; row_count?: number; error?: { message: string } }[],
): string {
  const ok = results.filter((item) => item.error === undefined);
  const failed = results.length - ok.length;
  const rows = ok.reduce((sum, item) => sum + (item.row_count ?? 0), 0);
  if (failed === 0) return `已导入 ${ok.length} 张表，共 ${rows} 行`;
  if (ok.length === 0) return `${failed} 张都没能导入`;
  return `已导入 ${ok.length} 张（${rows} 行），${failed} 张没能导入`;
}
