/**
 * 表达式的「免写」层。
 *
 * 实测（4 个真实项目的 active release）：63 个维度表达式**全部是裸列引用**，
 * 指标公式**一个都没有**。今天让人在一个单行输入框里手打 ``"门店名称"``——还得自己
 * 知道要加双引号——是把最常见的情况做成了最难的情况。
 *
 * 所以这里不做"让手写变容易"，做"大多数人不用写"：能读回单列引用的走列选择器，
 * 能读回二元式的走「指标 运算 指标」，读不回来的才落到自由文本。判据是确定性的
 * ——读不回来就老老实实转表达式模式，绝不把一条读不懂的口径改写成能画的形状。
 */

export type BinaryOperator = '+' | '-' | '*' | '/';

export const OPERATOR_LABEL: Record<BinaryOperator, string> = {
  '+': '＋ 相加',
  '-': '－ 相减',
  '*': '× 相乘',
  '/': '÷ 相除',
};

/** 标识符的字符集与 semantic-expression 一致：ASCII SQL 分隔符之外的都算名字的一部分。 */
const BARE = /^[^\s()+\-*/%,;'"=<>!|&^~?:[\]{}@#.]+$/u;

const normalize = (text: string) => text.trim().toLowerCase();

/** 读回单列引用；不是单列就返回 null。 */
export function readColumnExpr(expr: string, columns: readonly string[]): string | null {
  const text = expr.trim();
  if (!text) return null;
  const quoted = /^"((?:[^"]|"")*)"$/.exec(text);
  const candidate = quoted ? quoted[1].replace(/""/g, '"') : text;
  if (!quoted && !BARE.test(candidate)) return null;
  return columns.find((column) => normalize(column) === normalize(candidate)) ?? null;
}

/**
 * 一律带双引号写出去。
 *
 * 列名里出现全角括号（``营业收入（亿）``）时，不带引号会被表达式解析按分隔符切成
 * 两个不存在的字段——这条在 semantic-expression 的注释里已经栽过一次。
 */
export function writeColumnExpr(column: string): string {
  return `"${column.replace(/"/g, '""')}"`;
}

export interface BinaryExpr {
  left: string;
  operator: BinaryOperator;
  right: string;
}

/** 读回「名字 运算 名字」；两侧都必须是已知的指标名，否则返回 null。 */
export function readBinaryExpr(expr: string, names: readonly string[]): BinaryExpr | null {
  const match = /^\s*([^\s()+\-*/]+)\s*([+\-*/])\s*([^\s()+\-*/]+)\s*$/u.exec(expr);
  if (!match) return null;
  const known = (word: string) => names.find((name) => normalize(name) === normalize(word));
  const left = known(match[1]);
  const right = known(match[3]);
  if (!left || !right) return null;
  return { left, operator: match[2] as BinaryOperator, right };
}

export function writeBinaryExpr(parts: BinaryExpr): string {
  return `${parts.left} ${parts.operator} ${parts.right}`;
}

export interface Insertion {
  text: string;
  caret: number;
}

/**
 * 把一个名字插到光标处。
 *
 * 今天的「可用来源」芯片一律追加到末尾——光标停在表达式中间时点一下就把它拼坏了。
 * 两侧按需补空格，但紧贴括号时不补（``SUM(`` 后面补空格只是噪声）。
 */
export function insertAtCursor(
  text: string,
  selectionStart: number,
  selectionEnd: number,
  token: string,
): Insertion {
  const before = text.slice(0, selectionStart);
  const after = text.slice(selectionEnd);
  const needsLeft = before.length > 0 && !/[\s(]$/.test(before);
  const needsRight = after.length > 0 && !/^[\s)]/.test(after);
  const inserted = `${needsLeft ? ' ' : ''}${token}${needsRight ? ' ' : ''}`;
  return {
    text: `${before}${inserted}${after}`,
    caret: before.length + inserted.length - (needsRight ? 1 : 0),
  };
}
