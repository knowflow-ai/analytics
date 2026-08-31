import { useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, X } from 'lucide-react';
import type { AnalyticsCatalogHierarchy, AnalyticsDimension } from '@analytics/api/types';
import { Button, Field, Input } from '@analytics/components/ui';

/**
 * 维度层级编辑:同一把尺子由粗到细,例如 国家 → 省 → 市。
 *
 * 层级是模型内概念(levels 全是同模型维度),所以住在实体编辑器而不是画布。
 * 它告诉模型「按地区看」该落在哪一级、「再细一层」是什么;顺序即语义,
 * 所以用显式的上移/下移而不是靠用户记得拖拽。
 */

const splitList = (text: string) =>
  text.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);

export interface HierarchyEditorValues {
  name: string;
  aliases: string;
  levels: string[];
}

export function hierarchyInitial(hierarchy: AnalyticsCatalogHierarchy | null): HierarchyEditorValues {
  return {
    name: hierarchy?.name ?? '',
    aliases: (hierarchy?.alias ?? '').split(/[，,]/).filter(Boolean).join('，'),
    levels: hierarchy ? [...hierarchy.levels] : [],
  };
}

export function applyHierarchyValues(
  existing: AnalyticsCatalogHierarchy,
  values: HierarchyEditorValues,
): AnalyticsCatalogHierarchy {
  return {
    ...existing,
    name: values.name.trim(),
    alias: splitList(values.aliases).join(',') || null,
    levels: values.levels,
  };
}

export function HierarchyEditor({
  initial,
  dimensions,
  saving,
  onSave,
  onDelete,
  onClose,
}: {
  initial: HierarchyEditorValues;
  /** 本模型的维度(层级只能在同一模型内)。 */
  dimensions: AnalyticsDimension[];
  saving: boolean;
  onSave: (values: HierarchyEditorValues) => void;
  onDelete?: () => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState(initial);
  const byId = useMemo(() => new Map(dimensions.map((d) => [d.id, d])), [dimensions]);
  const available = dimensions.filter((d) => !form.levels.includes(d.id));
  const move = (index: number, delta: number) => {
    const next = [...form.levels];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setForm({ ...form, levels: next });
  };

  return (
    <div className="flex flex-col gap-3">
      <Field label="层级名称" hint="这组刻度的统称,例如「行政区划」「产品分类」">
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <Field label="别名" hint="用「，」分隔;用户说「按地区看」时靠它对上">
        <Input
          value={form.aliases}
          onChange={(e) => setForm({ ...form, aliases: e.target.value })}
        />
      </Field>
      <Field label="层级(由粗到细)" hint="顺序即语义:「再细一层」就是列表里的下一项">
        <div className="flex flex-col gap-1.5">
          {form.levels.map((id, index) => (
            <div
              key={id}
              className="flex items-center gap-2 rounded-md border border-slate-200 px-2.5 py-1.5 text-[13px]"
            >
              <span className="w-5 text-center font-mono text-[11px] text-slate-400">
                {index + 1}
              </span>
              <span className="flex-1 text-slate-800">{byId.get(id)?.name ?? id}</span>
              <button type="button" className="text-slate-400 hover:text-slate-600" onClick={() => move(index, -1)}>
                <ArrowUp className="h-3.5 w-3.5" />
              </button>
              <button type="button" className="text-slate-400 hover:text-slate-600" onClick={() => move(index, 1)}>
                <ArrowDown className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                className="text-slate-400 hover:text-red-600"
                onClick={() => setForm({ ...form, levels: form.levels.filter((x) => x !== id) })}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {form.levels.length < 2 && (
            <div className="text-[11px] text-slate-400">至少两级才构成层级。</div>
          )}
        </div>
        {available.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {available.map((d) => (
              <button
                key={d.id}
                type="button"
                onClick={() => setForm({ ...form, levels: [...form.levels, d.id] })}
                className="rounded-md border border-slate-200 px-2 py-1 text-[11px] text-slate-500 hover:border-slate-300"
              >
                + {d.name}
              </button>
            ))}
          </div>
        )}
      </Field>
      <div className="mt-1 flex items-center justify-between">
        {onDelete ? (
          <Button variant="ghost" onClick={onDelete}>
            删除层级
          </Button>
        ) : (
          <span />
        )}
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            loading={saving}
            disabled={!form.name.trim() || form.levels.length < 2}
            onClick={() => onSave(form)}
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  );
}
