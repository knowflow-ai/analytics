import { describe, expect, it } from 'vitest';
import {
  insertAtCursor,
  readBinaryExpr,
  readColumnExpr,
  writeBinaryExpr,
  writeColumnExpr,
} from './expression-builder';

const columns = ['门店名称', 'net_amount', '营业收入（亿）'];
const metrics = ['net_revenue', 'refund_amount', 'gross_profit'];

describe('维度表达式：绝大多数就是「选一列」', () => {
  it('读得回带引号的列引用', () => {
    /** 实测：4 个真实项目 63 个维度表达式，全部是裸列引用。 */
    expect(readColumnExpr('"门店名称"', columns)).toBe('门店名称');
    expect(readColumnExpr('  "net_amount" ', columns)).toBe('net_amount');
  });

  it('不带引号的写法也读得回来', () => {
    expect(readColumnExpr('net_amount', columns)).toBe('net_amount');
  });

  it('不是单列引用的一律读不回来，界面转表达式模式', () => {
    expect(readColumnExpr("CASE WHEN net_amount >= 1000 THEN '大额' ELSE '普通' END", columns)).toBeNull();
    expect(readColumnExpr('substr(order_no, 1, 4)', columns)).toBeNull();
    expect(readColumnExpr('unknown_col', columns)).toBeNull();
    expect(readColumnExpr('', columns)).toBeNull();
  });

  it('写出来一律带双引号——列名里有全角括号时不带引号会被切坏', () => {
    expect(writeColumnExpr('营业收入（亿）')).toBe('"营业收入（亿）"');
    expect(readColumnExpr(writeColumnExpr('营业收入（亿）'), columns)).toBe('营业收入（亿）');
  });
});

describe('复合指标：两个指标一个运算', () => {
  it('读得回二元式', () => {
    expect(readBinaryExpr('gross_profit / net_revenue', metrics)).toEqual({
      left: 'gross_profit',
      operator: '/',
      right: 'net_revenue',
    });
    expect(readBinaryExpr('net_revenue - refund_amount', metrics)).toEqual({
      left: 'net_revenue',
      operator: '-',
      right: 'refund_amount',
    });
  });

  it('三元、带常数、CASE WHEN 都读不回来，交给表达式模式', () => {
    expect(readBinaryExpr('net_revenue - refund_amount + gross_profit', metrics)).toBeNull();
    expect(readBinaryExpr('net_revenue * 2', metrics)).toBeNull();
    expect(readBinaryExpr('CASE WHEN net_revenue > 0 THEN 1 ELSE 0 END', metrics)).toBeNull();
    expect(readBinaryExpr('unknown - net_revenue', metrics)).toBeNull();
  });

  it('往返一致', () => {
    const parts = { left: 'gross_profit', operator: '/' as const, right: 'net_revenue' };
    expect(writeBinaryExpr(parts)).toBe('gross_profit / net_revenue');
    expect(readBinaryExpr(writeBinaryExpr(parts), metrics)).toEqual(parts);
  });
});

describe('插入到光标处，而不是追加到末尾', () => {
  it('插在选区位置并把光标放到插入内容之后', () => {
    /** 今天的芯片一律追加到末尾：光标在表达式中间时点一下就把它拼坏了。 */
    expect(insertAtCursor('a - b', 4, 4, 'x')).toEqual({ text: 'a - x b', caret: 5 });
  });

  it('选中一段时替换它', () => {
    expect(insertAtCursor('a - b', 4, 5, 'refund')).toEqual({ text: 'a - refund', caret: 10 });
  });

  it('两侧按需补空格，不会拼出 ab', () => {
    expect(insertAtCursor('a', 1, 1, 'b')).toEqual({ text: 'a b', caret: 3 });
    expect(insertAtCursor('', 0, 0, 'b')).toEqual({ text: 'b', caret: 1 });
    expect(insertAtCursor('a + ', 4, 4, 'b')).toEqual({ text: 'a + b', caret: 5 });
  });

  it('紧贴左括号时不补空格', () => {
    expect(insertAtCursor('SUM()', 4, 4, 'net_amount')).toEqual({
      text: 'SUM(net_amount)',
      caret: 14,
    });
  });
});
