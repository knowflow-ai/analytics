import { describe, expect, it } from 'vitest';

import {
  canCreate,
  canLoad,
  defaultTableName,
  isAcceptedFile,
  modeLabel,
  tableNameProblem,
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
