import { describe, expect, it } from 'vitest';
import { parseExpression, resolveAgainst } from './semantic-expression';

describe('表达式解析', () => {
  it('抽取字段引用,跳过函数名和关键字', () => {
    const parsed = parseExpression('SUM(CASE WHEN status_flag = 1 THEN net_amount ELSE 0 END)');
    expect(parsed.identifiers).toEqual(['status_flag', 'net_amount']);
    expect(parsed.hasAggregate).toBe(true);
  });

  it('不把字符串字面量和 SQL 关键字当字段', () => {
    /** 'paid' 是取值、WHERE 是关键字;误判会让用户看到莫名其妙的「不在可用来源里」。
     *  断言全量列表,避免遗漏的关键字被 toContain 悄悄放过。 */
    const parsed = parseExpression("SUM(net_amount) FILTER (WHERE status_flag = 'paid')");
    expect(parsed.identifiers).toEqual(['net_amount', 'status_flag']);
  });

  it('识别表名限定引用', () => {
    expect(parseExpression('orders.net_amount').hasQualified).toBe(true);
  });

  it('去重且保持出现顺序', () => {
    const parsed = parseExpression('net_amount - refund_amount + net_amount');
    expect(parsed.identifiers).toEqual(['net_amount', 'refund_amount']);
  });
});

describe('标识符边界', () => {
  it('全角括号是列名的一部分', () => {
    /** sqlglot 把「营业收入（亿）」整体当一个列;按字母数字下划线切会拆成两个
     *  不存在的字段,线上目录里这类列名很常见。 */
    expect(parseExpression('营业收入（亿）').identifiers).toEqual(['营业收入（亿）']);
    expect(parseExpression('营业收入（亿） - 净利润（亿）').identifiers).toEqual([
      '营业收入（亿）',
      '净利润（亿）',
    ]);
  });

  it('前导数字的列名不被切掉前缀', () => {
    expect(parseExpression('500强排名').identifiers).toEqual(['500强排名']);
  });

  it('数字字面量不算字段,且不把小数点当表名限定', () => {
    const parsed = parseExpression('net_amount * 0.5 + 1');
    expect(parsed.identifiers).toEqual(['net_amount']);
    expect(parsed.hasQualified).toBe(false);
  });

  it('转型的类型名不算字段', () => {
    /** amt::numeric 会被拆成两个 token,numeric 不是列;CAST(x AS numeric) 同理。 */
    expect(parseExpression('amt::numeric').identifiers).toEqual(['amt']);
    expect(parseExpression('created_at::date').identifiers).toEqual(['created_at']);
    expect(parseExpression('CAST(amt AS numeric)').identifiers).toEqual(['amt']);
  });

  it('双引号是标识符引用而不是字符串', () => {
    expect(parseExpression('"net amount" + 1').identifiers).toEqual(['net amount']);
  });
});

describe('引用解析', () => {
  it('大小写不敏感并规范化成目录里的写法', () => {
    const r = resolveAgainst(['NET_AMOUNT', 'Refund'], ['net_amount', 'refund'], '空');
    expect(r).toEqual({ error: null, resolved: ['net_amount', 'refund'] });
  });

  it('未知名字报错并列出可用清单', () => {
    const r = resolveAgainst(['gross'], ['net_amount'], '空');
    expect(r.error).toContain('gross');
    expect(r.error).toContain('net_amount');
  });

  it('没有任何引用时用调用方给的提示', () => {
    expect(resolveAgainst([], ['a'], '表达式至少要引用一个字段').error).toBe(
      '表达式至少要引用一个字段',
    );
  });
});
