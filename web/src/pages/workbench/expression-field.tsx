import { useRef } from 'react';
import { Textarea } from '@analytics/components/ui';
import { insertAtCursor } from './expression-builder';

/**
 * 自由文本表达式输入。只服务真正复杂的口径——单列引用与二元运算由各自的选择器
 * 免掉，读不回来的才落到这里。
 *
 * 与今天的差别只有两点，但都是被实机坑过的：名字**插到光标处**而不是追加到末尾
 * （光标停在中间时点一下就把表达式拼坏了），以及维度表达式从单行 Input 换成多行
 * ——一条 CASE WHEN 在单行框里根本看不全。
 */
export function ExpressionArea({
  value,
  rows = 2,
  tokens,
  emptyHint,
  error,
  onChange,
}: {
  value: string;
  rows?: number;
  /** 可插入的名字：``label`` 给人看，``token`` 写进表达式。 */
  tokens: Array<{ key: string; label: string; token: string }>;
  emptyHint: string;
  error?: string | null;
  onChange: (expr: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  const insert = (token: string) => {
    const node = ref.current;
    const at = node ? node.selectionStart : value.length;
    const to = node ? node.selectionEnd : value.length;
    const next = insertAtCursor(value, at, to, token);
    onChange(next.text);
    // 插完把光标放回插入内容之后,连着点两个名字才不会第二个跳到末尾。
    requestAnimationFrame(() => {
      node?.focus();
      node?.setSelectionRange(next.caret, next.caret);
    });
  };

  return (
    <div className="flex flex-col gap-1.5">
      <Textarea
        ref={ref}
        rows={rows}
        className="font-mono text-[12px]"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {error && <div className="text-[11px] text-red-600">{error}</div>}
      <div className="text-[11px] text-slate-500">
        点名字插到光标处：
        {tokens.length === 0 ? (
          <span className="text-slate-400">{emptyHint}</span>
        ) : (
          tokens.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => insert(item.token)}
              className="ml-1 rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600 hover:border-slate-300"
            >
              {item.label}
            </button>
          ))
        )}
      </div>
    </div>
  );
}
