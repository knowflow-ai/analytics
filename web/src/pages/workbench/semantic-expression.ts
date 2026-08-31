/**
 * 语义表达式的客户端解析,指标口径与维度表达式共用。
 *
 * 这是服务端 `modeling/semantic_expression.py` 的粗近似,只用于即时反馈和拼装
 * 请求体;权威判定仍在服务端,保存失败时以服务端错误为准。
 */

/** SQL 关键字与字面量不算字段引用。 */
const KEYWORDS = new Set([
  'and', 'or', 'not', 'null', 'is', 'in', 'like', 'between', 'case', 'when',
  'then', 'else', 'end', 'distinct', 'as', 'cast', 'interval', 'true', 'false',
  'asc', 'desc', 'date', 'timestamp', 'where', 'select', 'from', 'group', 'order',
  'by', 'having', 'on', 'over', 'partition', 'all', 'any', 'some', 'exists',
  // CAST(x AS numeric) 里的类型名不是字段引用。
  'numeric', 'decimal', 'integer', 'int', 'bigint', 'smallint', 'text', 'varchar',
  'char', 'boolean', 'bool', 'real', 'double', 'precision', 'float', 'json', 'jsonb',
  'uuid', 'time',
]);

const AGGREGATES = new Set(['sum', 'count', 'avg', 'min', 'max']);

export interface ExpressionRefs {
  /** 表达式引用的标识符,按出现顺序去重。 */
  identifiers: string[];
  /** 是否出现聚合函数。 */
  hasAggregate: boolean;
  /** 是否出现 `表名.字段` 这种限定引用(服务端一律拒绝)。 */
  hasQualified: boolean;
}

/**
 * 标识符的边界是 **ASCII 的 SQL 分隔符**,不是「字母数字下划线」。
 * sqlglot 把 `营业收入（亿）` 当成一个列标识符(全角括号不是 SQL 语法),
 * 按字符类切会把线上目录里这种列名拆成两个不存在的字段。
 */
const DELIMITERS = "\\s()+\\-*/%,;'\"=<>!|&^~?:\\[\\]{}@#.";
const TOKEN = new RegExp(
  `"((?:[^"]|"")*)"|([^${DELIMITERS}]+)\\s*(\\.)?\\s*(\\()?`,
  'gu',
);

/** PostgreSQL 的 `::type` 转型:类型名不是字段。 */
const CAST = /::\s*[\p{L}_][\p{L}\p{N}_]*/gu;

/** 数字字面量:两侧不能贴着标识符字符,否则 `500强排名` 会被切掉前缀。 */
const NUMERIC = /(^|[^\p{L}\p{N}_])\d+(\.\d+)?([eE][+-]?\d+)?(?![\p{L}\p{N}_])/gu;

/** 抽取表达式引用的标识符;跳过字符串字面量、数字和函数名。 */
export function parseExpression(expr: string): ExpressionRefs {
  const stripped = expr
    .replace(/'(?:[^']|'')*'/g, ' ')
    .replace(CAST, ' ')
    .replace(NUMERIC, '$1 ');
  const identifiers: string[] = [];
  const seen = new Set<string>();
  let hasAggregate = false;
  let hasQualified = false;
  TOKEN.lastIndex = 0;
  let match: RegExpExecArray | null;
  const push = (word: string) => {
    const lower = word.toLowerCase();
    if (seen.has(lower)) return;
    seen.add(lower);
    identifiers.push(word);
  };
  while ((match = TOKEN.exec(stripped)) !== null) {
    const [, quoted, word, dot, paren] = match;
    if (quoted !== undefined) {
      // 双引号在 PostgreSQL 里是标识符引用,不是字符串。
      push(quoted.replace(/""/g, '"'));
      continue;
    }
    if (word === undefined) continue;
    if (paren) {
      // 函数名,不是字段引用。
      if (AGGREGATES.has(word.toLowerCase())) hasAggregate = true;
      continue;
    }
    if (dot) {
      hasQualified = true;
      continue;
    }
    if (KEYWORDS.has(word.toLowerCase())) continue;
    push(word);
  }
  return { identifiers, hasAggregate, hasQualified };
}

export interface ReferenceResolution {
  /** 阻断保存的问题;为空表示可以提交。 */
  error: string | null;
  /** 引用到的名字,规范化为目录里的写法。 */
  resolved: string[];
}

/** 把标识符对照可用名解析,大小写不敏感;报错时带上可用清单。 */
export function resolveAgainst(
  identifiers: string[],
  available: string[],
  emptyMessage: string,
): ReferenceResolution {
  const byLower = new Map(available.map((name) => [name.toLowerCase(), name]));
  const unknown = identifiers.filter((id) => !byLower.has(id.toLowerCase()));
  if (unknown.length > 0) {
    const hint = available.length ? available.slice(0, 8).join('、') : '(没有可用的名字)';
    return { error: `${unknown.join('、')} 不在可用来源里。可用：${hint}`, resolved: [] };
  }
  if (identifiers.length === 0) return { error: emptyMessage, resolved: [] };
  return { error: null, resolved: identifiers.map((id) => byLower.get(id.toLowerCase()) as string) };
}
