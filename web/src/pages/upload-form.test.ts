import { describe, expect, it } from 'vitest';

import {
  canCreate,
  canImportPlan,
  canLoad,
  defaultTableName,
  defaultTableNames,
  isAcceptedFile,
  modeLabel,
  planProblems,
  summarizeOutcomes,
  tableNameProblem,
  type SheetPlanRow,
} from './upload-form';

describe('只收 .xlsx', () => {
  it('认后缀不认大小写', () => {
    expect(isAcceptedFile('门店台账.xlsx')).toBe(true);
    expect(isAcceptedFile('门店台账.XLSX')).toBe(true);
  });

  it('挡住核心侧读不了的格式', () => {
    // .xls 要另一颗解析依赖，让它传上去只会在服务端拿到一句读不出来。
    expect(isAcceptedFile('老台账.xls')).toBe(false);
    expect(isAcceptedFile('数据.csv')).toBe(false);
  });
});

describe('表名', () => {
  it('默认取文件名去掉后缀', () => {
    expect(defaultTableName('门店台账.xlsx')).toBe('门店台账');
  });

  it('同名要在前端就说清楚，而不是等服务端拒', () => {
    expect(tableNameProblem('门店台账', ['门店台账'])).toContain('已经有一张');
  });

  it('空名字和超长都不放行', () => {
    expect(tableNameProblem('  ', [])).toBeTruthy();
    expect(tableNameProblem('x'.repeat(64), [])).toContain('63');
  });

  it('可用的名字没有问题描述', () => {
    expect(tableNameProblem('新台账', ['门店台账'])).toBe('');
  });
});

describe('能不能提交', () => {
  const file = new File([''], 'a.xlsx');

  it('新建要文件、sheet、可用表名三样都齐', () => {
    expect(canCreate({ file, sheet: '表', table: '新表', existing: [] })).toBe(true);
    expect(canCreate({ file: null, sheet: '表', table: '新表', existing: [] })).toBe(false);
    expect(canCreate({ file, sheet: '', table: '新表', existing: [] })).toBe(false);
    expect(canCreate({ file, sheet: '表', table: '新表', existing: ['新表'] })).toBe(false);
  });

  it('灌数不校验重名——目标本来就得是已有的表', () => {
    expect(canLoad({ file, sheet: '表', table: '门店台账' })).toBe(true);
    expect(canLoad({ file, sheet: '表', table: '' })).toBe(false);
  });
});

describe('模式说人话', () => {
  it('替换要说清是整表换掉', () => {
    expect(modeLabel('replace')).toContain('全部');
    expect(modeLabel('append')).toContain('追加');
  });
});

describe('多 sheet 一次导入', () => {
  const row = (sheet: string, table: string, extra: Partial<SheetPlanRow> = {}): SheetPlanRow => ({
    sheet,
    table,
    selected: true,
    ...extra,
  });

  it('多张时表名默认取 sheet 名，一张时取文件名', () => {
    expect(defaultTableNames('台账.xlsx', ['销售'])).toEqual({ 销售: '台账' });
    expect(defaultTableNames('台账.xlsx', ['销售', '档案'])).toEqual({
      销售: '销售',
      档案: '档案',
    });
  });

  it('同一批里重名要当场说出来', () => {
    // 不拦的话先建的会让后建的撞上"已存在"，报出来的原因和真正的错因对不上。
    const problems = planProblems([row('一', '台账'), row('二', '台账')], []);
    expect(problems['一']).toContain('另一张表');
    expect(problems['二']).toContain('另一张表');
  });

  it('和已有表重名同样拦下', () => {
    expect(planProblems([row('一', '台账')], ['台账'])['一']).toContain('已经有一张');
  });

  it('没勾的和读不出来的不参与校验', () => {
    const rows = [
      row('一', '台账'),
      row('二', '台账', { selected: false }),
      row('三', '台账', { blocked: '只有表头' }),
    ];
    expect(planProblems(rows, [])).toEqual({});
    expect(canImportPlan(rows, [])).toBe(true);
  });

  it('一张都没勾就不能提交', () => {
    expect(canImportPlan([row('一', '台账', { selected: false })], [])).toBe(false);
  });

  it('结果要说清几张成、几张败', () => {
    expect(summarizeOutcomes([{ table: 'a', row_count: 5 }, { table: 'b', row_count: 3 }]))
      .toBe('已导入 2 张表，共 8 行');
    expect(summarizeOutcomes([{ table: 'a', row_count: 5 }, { table: 'b', error: { message: 'x' } }]))
      .toContain('1 张没能导入');
    expect(summarizeOutcomes([{ table: 'a', error: { message: 'x' } }])).toContain('都没能导入');
  });
});
