import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Database, Table2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { createDraft, extendTables, listSchemas, listTables, versionOf } from '@analytics/api/analytics';
import type { AnalyticsRevision } from '@analytics/api/types';
import { Button, Empty, Input, Select, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

interface Props {
  projectId: string;
  /** null while the project has no revision yet: the panel is in import mode. */
  revision: AnalyticsRevision | null;
  acceptRevision: (next: AnalyticsRevision) => void;
  readOnly?: boolean;
}

/**
 * Table import and extension. Both go through the same picker: the first
 * import creates the revision, later picks append to it (the core rescans old
 * tables as well and fails closed on drift).
 */
export function TablesPanel({ projectId, revision, acceptRevision, readOnly }: Props) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const schemas = useQuery({ queryKey: ['schemas', projectId], queryFn: () => listSchemas(projectId) });
  const [schema, setSchema] = useState('');
  const [includeViews, setIncludeViews] = useState(false);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // 优先 public：多数 PostgreSQL 库把业务表放在那儿。服务端已经不再返回空 schema，
  // 所以 public 出现在列表里就意味着它真的有表——空的时候会自然落到第一个有表的。
  useEffect(() => {
    if (!schema && schemas.data?.items.length) {
      setSchema(schemas.data.items.includes('public') ? 'public' : schemas.data.items[0]);
    }
  }, [schema, schemas.data]);

  const tables = useQuery({
    queryKey: ['tables', projectId, schema, includeViews],
    queryFn: () => listTables(projectId, schema, includeViews),
    enabled: Boolean(schema),
  });

  const imported = useMemo(
    () =>
      new Set(
        (revision?.semantic_spec.models ?? [])
          .filter((model) => model.table)
          .map((model) => `${model.schema_name}.${model.table}`),
      ),
    [revision],
  );

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (tables.data?.items ?? []).filter(
      (item) => !needle || item.name.toLowerCase().includes(needle) || item.comment.toLowerCase().includes(needle),
    );
  }, [filter, tables.data]);

  const toggle = (key: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const submit = useMutation({
    mutationFn: async () => {
      const bySchema: Record<string, string[]> = {};
      selected.forEach((key) => {
        const [schemaName, tableName] = key.split('.', 2);
        (bySchema[schemaName] ??= []).push(tableName);
      });
      if (revision) {
        return extendTables(projectId, revision.id, {
          ...versionOf(revision),
          selected_tables: bySchema,
          include_views: includeViews,
        });
      }
      return createDraft(projectId, {
        schemas: Object.keys(bySchema),
        selected_tables: bySchema,
        include_views: includeViews,
      });
    },
    onSuccess: (next) => {
      acceptRevision(next);
      setSelected(new Set());
      toast.success(
        `已导入 ${selected.size} 张表。接下来进入语义建模，运行 AI 补全或手工核对目录。`,
      );
    },
    onError: (error) => {
      toast.error(describeError(error));
      // A draft import is several calls; if it failed after the revision was
      // created, reload so the panel continues in extend mode instead of
      // creating a second revision next time.
      queryClient.invalidateQueries({ queryKey: ['summary', projectId] });
    },
  });

  return (
    // h-full：容器现在是固定高度（四个 tab 一致），面板不撑满的话左侧那条分割线
    // 只画到内容末尾就断了，下面空一截。
    <div className="grid h-full min-h-[560px] grid-cols-[280px_1fr]">
      <aside className="border-r border-slate-100 p-4">
        <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          <Database className="h-3.5 w-3.5" /> 数据源
        </div>
        {schemas.isPending && <Spinner />}
        {schemas.isError && (
          <div className="text-xs text-red-600">{describeError(schemas.error)}</div>
        )}
        {schemas.data && (
          <>
            <label className="mb-1 block text-xs text-slate-500">Schema</label>
            <Select value={schema} onChange={(event) => setSchema(event.target.value)}>
              {schemas.data.items.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </Select>
            <label className="mt-3 flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={includeViews}
                onChange={(event) => setIncludeViews(event.target.checked)}
              />
              包含视图
            </label>
          </>
        )}
        {revision && (
          <div className="mt-6">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              已导入 {imported.size}
            </div>
            <ul className="flex flex-col gap-1 text-xs text-slate-600">
              {revision.semantic_spec.models.map((model) => (
                <li key={model.id} className="flex items-center gap-1.5 truncate">
                  <Table2 className="h-3 w-3 shrink-0 text-slate-400" />
                  <span className="truncate">{model.name}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </aside>

      <section className="flex flex-col">
        <div className="flex items-center gap-3 border-b border-slate-100 px-4 py-3">
          <div className="w-72">
            <Input
              placeholder="搜索表名或注释"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </div>
          <div className="ml-auto text-xs text-slate-400">已选 {selected.size} 张</div>
          <Button
            variant="primary"
            size="sm"
            disabled={readOnly || selected.size === 0}
            loading={submit.isPending}
            onClick={() => submit.mutate()}
          >
            {revision ? '追加到模型' : '导入并开始建模'}
          </Button>
        </div>
        {tables.isPending && schema && <Spinner />}
        {tables.isError && (
          <div className="p-4 text-xs text-red-600">{describeError(tables.error)}</div>
        )}
        {tables.data && visible.length === 0 && (
          <Empty title="没有匹配的表" hint="换一个 schema，或勾选「包含视图」。" />
        )}
        {tables.data && visible.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {visible.map((item) => {
              const key = `${item.schema_name}.${item.name}`;
              const already = imported.has(key);
              return (
                <li key={key}>
                  <label
                    className={`flex cursor-pointer items-center gap-3 px-4 py-2.5 hover:bg-slate-50 ${
                      already ? 'opacity-50' : ''
                    }`}
                  >
                    <input
                      type="checkbox"
                      disabled={already || readOnly}
                      checked={already || selected.has(key)}
                      onChange={() => toggle(key)}
                    />
                    <Table2 className="h-4 w-4 text-slate-400" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[13px] text-slate-800">
                        {item.name}
                        {item.source_type === 'view' && (
                          <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-500">
                            视图
                          </span>
                        )}
                        {already && <span className="text-[11px] text-slate-400">已导入</span>}
                      </div>
                      {item.comment && (
                        <div className="truncate text-xs text-slate-400">{item.comment}</div>
                      )}
                    </div>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
