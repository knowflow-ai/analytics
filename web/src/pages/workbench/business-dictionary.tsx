import { useMutation } from "@tanstack/react-query";
import { BookOpenText, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  deleteCatalogResource,
  newResourceId,
  saveDimensionValue,
  saveTerm,
  versionOf,
} from "@analytics/api/analytics";
import type {
  AnalyticsDimensionValue,
  AnalyticsTerm,
} from "@analytics/api/types";
import {
  Badge,
  Button,
  Dialog,
  Empty,
  Field,
  Input,
  Textarea,
  useToast,
} from "@analytics/components/ui";
import { describeError } from "@analytics/lib/labels";
import type { WorkbenchContext } from "./index";

type TermEditorContext = Pick<
  WorkbenchContext,
  "projectId" | "revision" | "acceptRevision" | "readOnly"
>;

export type BusinessDictionarySection = "terms" | "dimensionValues";

export const BUSINESS_DICTIONARY_SECTIONS: ReadonlyArray<{
  key: BusinessDictionarySection;
  label: string;
}> = [
  { key: "terms", label: "业务术语" },
  { key: "dimensionValues", label: "维度值字典" },
];

export interface TermBindingPreset {
  metricId?: string;
  dimensionId?: string;
}

export interface TermDraft {
  name: string;
  description: string;
  aliasesText: string;
  metricIds: string[];
  dimensionIds: string[];
}

export interface DimensionValueDraft {
  displayName: string;
  aliasesText: string;
  enabled: boolean;
}

const unique = (items: string[]) => [...new Set(items.filter(Boolean))];

export function createTermDraft(
  term?: AnalyticsTerm,
  preset: TermBindingPreset = {},
): TermDraft {
  return {
    name: term?.name ?? "",
    description: term?.description ?? "",
    aliasesText: term?.aliases.join("，") ?? "",
    metricIds: unique([
      ...(term?.metric_ids ?? []),
      ...(preset.metricId ? [preset.metricId] : []),
    ]),
    dimensionIds: unique([
      ...(term?.dimension_ids ?? []),
      ...(preset.dimensionId ? [preset.dimensionId] : []),
    ]),
  };
}

export function validateTermDraft(draft: TermDraft): string | null {
  if (!draft.name.trim()) return "请输入术语名称";
  if (draft.metricIds.length === 0 && draft.dimensionIds.length === 0) {
    return "至少关联一个指标或维度";
  }
  return null;
}

function normalizedAliases(value: string): string[] {
  const seen = new Set<string>();
  return value
    .split(/[，,、\n]/)
    .map((item) => item.trim())
    .filter((item) => {
      const key = item.toLocaleLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function createDimensionValueDraft(
  value: AnalyticsDimensionValue,
): DimensionValueDraft {
  return {
    displayName: value.display_name,
    aliasesText: value.aliases.join("，"),
    enabled: value.enabled,
  };
}

export function dimensionValueResourceFromDraft(
  value: AnalyticsDimensionValue,
  draft: DimensionValueDraft,
): AnalyticsDimensionValue {
  return {
    ...value,
    display_name: draft.displayName.trim(),
    aliases: normalizedAliases(draft.aliasesText),
    enabled: draft.enabled,
  };
}

export function termResourceFromDraft(
  id: string,
  draft: TermDraft,
  existing?: AnalyticsTerm,
): AnalyticsTerm {
  return {
    id,
    name: draft.name.trim(),
    description: draft.description.trim(),
    aliases: normalizedAliases(draft.aliasesText),
    // Compatibility QueryScope links are compiler-owned. They stay hidden and
    // survive an edit; a newly created term starts without them.
    dataset_ids: existing?.dataset_ids ?? [],
    metric_ids: unique(draft.metricIds),
    dimension_ids: unique(draft.dimensionIds),
  };
}

export function TermEditorDialog({
  context,
  term,
  preset,
  open,
  onClose,
}: {
  context: TermEditorContext;
  term?: AnalyticsTerm;
  preset?: TermBindingPreset;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      title={term ? "编辑业务术语" : "新建业务术语"}
      onClose={onClose}
      width="max-w-2xl"
    >
      {open && (
        <TermEditorForm
          key={`${term?.id ?? "new"}:${preset?.metricId ?? ""}:${preset?.dimensionId ?? ""}`}
          context={context}
          term={term}
          preset={preset}
          onClose={onClose}
        />
      )}
    </Dialog>
  );
}

function TermEditorForm({
  context,
  term,
  preset,
  onClose,
}: {
  context: TermEditorContext;
  term?: AnalyticsTerm;
  preset?: TermBindingPreset;
  onClose: () => void;
}) {
  const { projectId, revision, acceptRevision, readOnly } = context;
  const toast = useToast();
  const [draft, setDraft] = useState<TermDraft>(() =>
    createTermDraft(term, preset),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () =>
      saveTerm(
        projectId,
        revision.id,
        versionOf(revision),
        termResourceFromDraft(term?.id ?? newResourceId("term"), draft, term),
      ),
    onSuccess: (next) => {
      acceptRevision(next);
      toast.success(term ? "业务术语已更新。" : "业务术语已创建。");
      onClose();
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const toggle = (key: "metricIds" | "dimensionIds", id: string) => {
    const values = draft[key];
    setDraft({
      ...draft,
      [key]: values.includes(id)
        ? values.filter((value) => value !== id)
        : [...values, id],
    });
    setValidationError(null);
  };
  const submit = () => {
    const error = validateTermDraft(draft);
    setValidationError(error);
    if (!error) save.mutate();
  };

  return (
    <div className="flex flex-col gap-4">
      <Field label="术语名称">
        <Input
          autoFocus
          disabled={readOnly}
          placeholder="例如：成交额"
          value={draft.name}
          onChange={(event) => {
            setDraft({ ...draft, name: event.target.value });
            setValidationError(null);
          }}
        />
      </Field>
      <Field label="同义说法" hint="用「，」分隔，例如 GMV、流水、交易额">
        <Input
          disabled={readOnly}
          value={draft.aliasesText}
          onChange={(event) =>
            setDraft({ ...draft, aliasesText: event.target.value })
          }
        />
      </Field>
      <Field label="业务定义" hint="说明业务含义、统计边界和容易混淆的口径。">
        <Textarea
          rows={3}
          disabled={readOnly}
          placeholder="例如：用户实际支付且支付成功的订单金额。"
          value={draft.description}
          onChange={(event) =>
            setDraft({ ...draft, description: event.target.value })
          }
        />
      </Field>
      <div className="grid gap-4 md:grid-cols-2">
        <BindingList
          title="关联指标"
          empty="当前目录还没有指标，请先运行 AI 补全或维护实体。"
          items={revision.semantic_spec.metrics.map((item) => ({
            id: item.id,
            name: item.name,
          }))}
          selected={draft.metricIds}
          disabled={readOnly}
          onToggle={(id) => toggle("metricIds", id)}
        />
        <BindingList
          title="关联维度"
          empty="当前目录还没有维度，请先运行 AI 补全或维护实体。"
          items={revision.semantic_spec.dimensions.map((item) => ({
            id: item.id,
            name: item.name,
          }))}
          selected={draft.dimensionIds}
          disabled={readOnly}
          onToggle={(id) => toggle("dimensionIds", id)}
        />
      </div>
      {validationError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {validationError}
        </div>
      )}
      <div className="flex justify-end gap-2 border-t border-slate-100 pt-3">
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="primary"
          loading={save.isPending}
          disabled={readOnly}
          onClick={submit}
        >
          保存到当前草稿
        </Button>
      </div>
    </div>
  );
}

function DimensionValueEditorDialog({
  context,
  value,
  dimensionName,
  onClose,
}: {
  context: TermEditorContext;
  value?: AnalyticsDimensionValue;
  dimensionName: string;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={Boolean(value)}
      title="编辑维度值"
      onClose={onClose}
      width="max-w-lg"
    >
      {value && (
        <DimensionValueEditorForm
          key={value.id}
          context={context}
          value={value}
          dimensionName={dimensionName}
          onClose={onClose}
        />
      )}
    </Dialog>
  );
}

function DimensionValueEditorForm({
  context,
  value,
  dimensionName,
  onClose,
}: {
  context: TermEditorContext;
  value: AnalyticsDimensionValue;
  dimensionName: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<DimensionValueDraft>(() =>
    createDimensionValueDraft(value),
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () =>
      saveDimensionValue(
        context.projectId,
        context.revision.id,
        versionOf(context.revision),
        dimensionValueResourceFromDraft(value, draft),
      ),
    onSuccess: (next) => {
      context.acceptRevision(next);
      toast.success("维度值已更新。");
      onClose();
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const submit = () => {
    if (!draft.displayName.trim()) {
      setValidationError("请输入展示名称");
      return;
    }
    setValidationError(null);
    save.mutate();
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 rounded-md bg-slate-50 px-3 py-2 text-xs">
        <div>
          <div className="text-[10px] text-slate-400">所属维度</div>
          <div className="mt-0.5 text-slate-700">{dimensionName}</div>
        </div>
        <div>
          <div className="text-[10px] text-slate-400">原始值（不可修改）</div>
          <div className="mt-0.5 font-mono text-slate-700">
            {String(value.value)}
          </div>
        </div>
      </div>
      <Field label="展示名称">
        <Input
          autoFocus
          disabled={context.readOnly}
          value={draft.displayName}
          onChange={(event) => {
            setDraft({ ...draft, displayName: event.target.value });
            setValidationError(null);
          }}
        />
      </Field>
      <Field label="同义值" hint="用「，」分隔，例如 华东、东区、East China">
        <Input
          disabled={context.readOnly}
          value={draft.aliasesText}
          onChange={(event) =>
            setDraft({ ...draft, aliasesText: event.target.value })
          }
        />
      </Field>
      <label className="flex items-center gap-2 text-xs text-slate-700">
        <input
          type="checkbox"
          disabled={context.readOnly}
          checked={draft.enabled}
          onChange={(event) =>
            setDraft({ ...draft, enabled: event.target.checked })
          }
        />
        问数时可匹配这个维度值
      </label>
      {validationError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {validationError}
        </div>
      )}
      <div className="flex justify-end gap-2 border-t border-slate-100 pt-3">
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="primary"
          loading={save.isPending}
          disabled={context.readOnly}
          onClick={submit}
        >
          保存到当前草稿
        </Button>
      </div>
    </div>
  );
}

function BindingList({
  title,
  empty,
  items,
  selected,
  disabled,
  onToggle,
}: {
  title: string;
  empty: string;
  items: Array<{ id: string; name: string }>;
  selected: string[];
  disabled: boolean;
  onToggle: (id: string) => void;
}) {
  return (
    <fieldset className="min-w-0 rounded-md border border-slate-200 px-3 py-2">
      <legend className="px-1 text-xs font-medium text-slate-600">
        {title}
      </legend>
      {items.length === 0 ? (
        <div className="py-2 text-[11px] text-slate-400">{empty}</div>
      ) : (
        <div className="max-h-36 space-y-1 overflow-auto py-1">
          {items.map((item) => (
            <label
              key={item.id}
              className="flex items-center gap-2 text-xs text-slate-700"
            >
              <input
                type="checkbox"
                disabled={disabled}
                checked={selected.includes(item.id)}
                onChange={() => onToggle(item.id)}
              />
              <span className="truncate">{item.name}</span>
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

export function BusinessDictionaryPanel({
  context,
  section,
  onSectionChange,
  onOpenGraph,
}: {
  context: TermEditorContext;
  section: BusinessDictionarySection;
  onSectionChange: (section: BusinessDictionarySection) => void;
  onOpenGraph: () => void;
}) {
  const { revision, readOnly, projectId, acceptRevision } = context;
  const toast = useToast();
  const terms = revision.semantic_catalog.terms;
  const values = revision.semantic_catalog.dimensionValues;
  const [editorTerm, setEditorTerm] = useState<AnalyticsTerm | null>();
  const [editorValue, setEditorValue] = useState<AnalyticsDimensionValue>();
  const metricNames = useMemo(
    () =>
      new Map(
        revision.semantic_spec.metrics.map((item) => [item.id, item.name]),
      ),
    [revision.semantic_spec.metrics],
  );
  const dimensionNames = useMemo(
    () =>
      new Map(
        revision.semantic_spec.dimensions.map((item) => [item.id, item.name]),
      ),
    [revision.semantic_spec.dimensions],
  );
  const remove = useMutation({
    mutationFn: (term: AnalyticsTerm) =>
      deleteCatalogResource(
        projectId,
        revision.id,
        versionOf(revision),
        "terms",
        term.id,
      ),
    onSuccess: (next) => {
      acceptRevision(next);
      toast.success("业务术语已删除。");
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const describeBindings = (term: AnalyticsTerm) => [
    ...term.metric_ids.map((id) => metricNames.get(id) ?? id),
    ...term.dimension_ids.map((id) => dimensionNames.get(id) ?? id),
  ];

  return (
    <div className="px-6 py-5">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">业务词典</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            维护业务人员会怎么说，并把这些说法绑定到受治理指标、维度和真实维度值。
          </p>
        </div>
        {section === "terms" && !readOnly && (
          <Button
            variant="primary"
            size="sm"
            icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => setEditorTerm(null)}
          >
            新建术语
          </Button>
        )}
      </div>

      <div className="mb-4 flex items-center gap-1 border-b border-slate-100">
        {BUSINESS_DICTIONARY_SECTIONS.map((item) => {
          const count = item.key === "terms" ? terms.length : values.length;
          return (
            <button
              key={item.key}
              type="button"
              aria-current={section === item.key ? "page" : undefined}
              className={`border-b-2 px-3 py-2 text-xs font-medium transition-colors ${
                section === item.key
                  ? "border-blue-600 text-blue-700"
                  : "border-transparent text-slate-500 hover:text-slate-700"
              }`}
              onClick={() => onSectionChange(item.key)}
            >
              {item.label}（{count}）
            </button>
          );
        })}
      </div>

      {section === "terms" ? (
        terms.length === 0 ? (
          <Empty
            title="还没有业务术语"
            hint="把“成交额、流水、GMV”这类业务说法绑定到已有指标或维度，问数时才能确定性识别。"
            action={
              !readOnly && (
                <Button variant="primary" onClick={() => setEditorTerm(null)}>
                  新建术语
                </Button>
              )
            }
          />
        ) : (
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
            {terms.map((term) => (
              <li key={term.id} className="px-3 py-3 text-xs">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="font-medium text-slate-800">
                      {term.name}
                    </div>
                    {term.description && (
                      <div className="mt-1 whitespace-pre-wrap leading-relaxed text-slate-500">
                        {term.description}
                      </div>
                    )}
                  </div>
                  {!readOnly && (
                    <div className="flex shrink-0 gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={<Pencil className="h-3.5 w-3.5" />}
                        onClick={() => setEditorTerm(term)}
                      >
                        编辑
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        icon={<Trash2 className="h-3.5 w-3.5" />}
                        loading={remove.isPending}
                        onClick={() => {
                          if (window.confirm(`删除业务术语「${term.name}」？`))
                            remove.mutate(term);
                        }}
                      >
                        删除
                      </Button>
                    </div>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-1">
                  <span className="text-[10px] text-slate-400">同义说法</span>
                  {term.aliases.length > 0 ? (
                    term.aliases.map((alias) => (
                      <Badge key={alias}>{alias}</Badge>
                    ))
                  ) : (
                    <span className="text-[11px] text-slate-400">无</span>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-1">
                  <span className="text-[10px] text-slate-400">关联资源</span>
                  {describeBindings(term).map((name) => (
                    <Badge key={name} tone="blue" variant="outline">
                      {name}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )
      ) : (
        <DimensionValueList
          values={values}
          dimensionNames={dimensionNames}
          readOnly={readOnly}
          onEdit={setEditorValue}
          onOpenGraph={onOpenGraph}
        />
      )}

      <TermEditorDialog
        context={context}
        term={editorTerm ?? undefined}
        open={editorTerm !== undefined}
        onClose={() => setEditorTerm(undefined)}
      />
      <DimensionValueEditorDialog
        context={context}
        value={editorValue}
        dimensionName={
          editorValue
            ? dimensionNames.get(editorValue.dimension_id) ??
              editorValue.dimension_id
            : ""
        }
        onClose={() => setEditorValue(undefined)}
      />
    </div>
  );
}

function DimensionValueList({
  values,
  dimensionNames,
  readOnly,
  onEdit,
  onOpenGraph,
}: {
  values: AnalyticsDimensionValue[];
  dimensionNames: ReadonlyMap<string, string>;
  readOnly: boolean;
  onEdit: (value: AnalyticsDimensionValue) => void;
  onOpenGraph: () => void;
}) {
  if (values.length === 0) {
    return (
      <Empty
        title="还没有维度值字典"
        hint="进入实体与关系，选择一个维度后从真实数据采集取值，再审核展示名与同义值。"
        action={<Button onClick={onOpenGraph}>去实体与关系采集</Button>}
      />
    );
  }
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3 text-xs text-slate-500">
        <span>维度值来自真实数据采集；展示名称与同义值在所属维度中审核。</span>
        <Button size="sm" onClick={onOpenGraph}>
          管理维度值
        </Button>
      </div>
      <div className="overflow-auto rounded-lg border border-slate-200">
        <table className="w-full min-w-[680px] text-left text-xs">
          <thead className="bg-slate-50 text-[11px] text-slate-400">
            <tr>
              <th className="px-3 py-2 font-medium">维度</th>
              <th className="px-3 py-2 font-medium">原始值</th>
              <th className="px-3 py-2 font-medium">展示名称</th>
              <th className="px-3 py-2 font-medium">同义值</th>
              <th className="px-3 py-2 font-medium">状态</th>
              {!readOnly && <th className="px-3 py-2 font-medium">操作</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {values.map((value) => (
              <tr key={value.id}>
                <td className="px-3 py-2 text-slate-700">
                  {dimensionNames.get(value.dimension_id) ?? value.dimension_id}
                </td>
                <td className="px-3 py-2 font-mono text-slate-600">
                  {String(value.value)}
                </td>
                <td className="px-3 py-2 text-slate-700">
                  {value.display_name}
                </td>
                <td className="px-3 py-2 text-slate-500">
                  {value.aliases.join("、") || "—"}
                </td>
                <td className="px-3 py-2">
                  <Badge tone={value.enabled ? "green" : "slate"}>
                    {value.enabled ? "问数可用" : "已停用"}
                  </Badge>
                </td>
                {!readOnly && (
                  <td className="px-3 py-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<Pencil className="h-3.5 w-3.5" />}
                      onClick={() => onEdit(value)}
                    >
                      编辑
                    </Button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ContextualTermButton({
  context,
  preset,
}: {
  context: TermEditorContext;
  preset: TermBindingPreset;
}) {
  const [open, setOpen] = useState(false);
  if (context.readOnly) return null;
  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        icon={<BookOpenText className="h-3.5 w-3.5" />}
        onClick={() => setOpen(true)}
      >
        添加业务术语
      </Button>
      <TermEditorDialog
        context={context}
        preset={preset}
        open={open}
        onClose={() => setOpen(false)}
      />
    </>
  );
}
