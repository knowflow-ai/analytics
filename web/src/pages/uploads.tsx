import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trash2, Upload } from 'lucide-react';
import { useState } from 'react';

import {
  commitUpload,
  deleteUpload,
  inspectUpload,
  listUploads,
  loadUpload,
  type UploadPreview,
  type UploadedTable,
} from '@analytics/api/analytics';
import {
  Button,
  ConfirmationDialog,
  Dialog,
  Empty,
  ErrorBanner,
  Field,
  Input,
  Select,
  Spinner,
  useToast,
} from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import {
  ACCEPTED_SUFFIX,
  canImportPlan,
  canLoad,
  defaultTableNames,
  isAcceptedFile,
  modeLabel,
  planProblems,
  summarizeOutcomes,
  type SheetPlanRow,
  type UploadMode,
} from './upload-form';

/**
 * 上传表格。
 *
 * 上传的表落在同一个 PostgreSQL 实例的独立库里，并自动成为一条名为「上传的表格」的
 * 数据源——项目绑定它之后，扫表、建模、问数走的是和数据库数据源完全相同的链路。
 *
 * **落库前必须先让用户看到自动改了什么。** 脏表头（重名、空、首尾空格、整列空）会被
 * 规范化，不说出来的话他会在建模页对着一个自己没写过的列名。
 */

export function UploadsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [importing, setImporting] = useState(false);
  const [removing, setRemoving] = useState<UploadedTable | null>(null);

  const tables = useQuery({
    queryKey: ['analytics-uploads'],
    queryFn: listUploads,
    enabled: open,
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['analytics-uploads'] });
    // 首次上传会新建一条数据源，绑定下拉要能立刻看到它。
    queryClient.invalidateQueries({ queryKey: ['analytics-data-sources'] });
  };

  const remove = useMutation({
    mutationFn: (item: UploadedTable) => deleteUpload(item.table),
    onSuccess: () => {
      setRemoving(null);
      toast.success('表已删除');
      refresh();
    },
    // 被已发布模型用着时服务端会拒（UPLOAD_TABLE_IN_USE），那条消息比"删除失败"有用。
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <>
      <Dialog
        open={open}
        title="上传的表格"
        onClose={onClose}
        footer={<Button onClick={onClose}>关闭</Button>}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <p className="min-w-0 text-xs leading-relaxed text-slate-400">
            上传的表会存成一张真实的表，并归入「上传的表格」这个数据源。项目绑定它之后，
            建模与问数和数据库数据源完全一样。
          </p>
          <Button
            variant="primary"
            icon={<Upload className="h-4 w-4" />}
            onClick={() => setImporting(true)}
          >
            上传
          </Button>
        </div>

        {tables.isPending && <Spinner />}
        {tables.isError && <ErrorBanner message={describeError(tables.error)} />}
        {tables.data && tables.data.length === 0 && (
          <Empty title="还没有上传过表格" hint={`支持 ${ACCEPTED_SUFFIX} 文件。`} />
        )}
        {tables.data && tables.data.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {tables.data.map((item) => (
              <li key={item.table} className="flex items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <div className="truncate text-sm text-slate-900">{item.table}</div>
                  <div className="mt-0.5 text-xs text-slate-400">
                    {item.row_count} 行 · {item.columns.length} 列
                  </div>
                </div>
                <Button
                  icon={<Trash2 className="h-4 w-4" />}
                  onClick={() => setRemoving(item)}
                >
                  删除
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Dialog>

      <UploadDialog
        open={importing}
        existing={tables.data ?? []}
        onClose={() => setImporting(false)}
        onDone={() => {
          setImporting(false);
          refresh();
        }}
      />

      {removing && (
        <ConfirmationDialog
          open
          title="删除表格"
          description={`删除「${removing.table}」？已经被建模用上的表不会删成功。`}
          confirmText="删除"
          danger
          loading={remove.isPending}
          onClose={() => setRemoving(null)}
          onConfirm={() => remove.mutate(removing)}
        />
      )}
    </>
  );
}

/** 选文件 → 勾要导的 sheet → 看确认表 → 一次落库。 */
function UploadDialog({
  open,
  existing,
  onClose,
  onDone,
}: {
  open: boolean;
  existing: UploadedTable[];
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [previews, setPreviews] = useState<UploadPreview[]>([]);
  const [rows, setRows] = useState<SheetPlanRow[]>([]);
  const [mode, setMode] = useState<UploadMode>('create');
  const [target, setTarget] = useState('');
  const [loadSheet, setLoadSheet] = useState('');
  const [error, setError] = useState('');

  const reset = () => {
    setFile(null);
    setPreviews([]);
    setRows([]);
    setMode('create');
    setTarget('');
    setLoadSheet('');
    setError('');
  };

  const pick = useMutation({
    mutationFn: async (picked: File) => ({ picked, result: await inspectUpload(picked) }),
    onSuccess: ({ picked, result }) => {
      const names = defaultTableNames(picked.name, result.sheets);
      setFile(picked);
      setPreviews(result.previews);
      setRows(
        result.previews.map((item) => ({
          sheet: item.sheet,
          table: names[item.sheet] ?? item.sheet,
          // 读不出来的默认不勾，也勾不上——但它要留在列表里，否则用户不知道
          // 自己文件里那张表去哪了。
          selected: item.error === undefined,
          blocked: item.error?.message,
        })),
      );
      setLoadSheet(result.previews.find((item) => !item.error)?.sheet ?? '');
      setTarget(existing[0]?.table ?? '');
      setError('');
    },
    onError: (issue) => {
      reset();
      setError(describeError(issue));
    },
  });

  const submit = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error('还没有选文件');
      if (mode === 'create') {
        return commitUpload(
          file,
          rows
            .filter((row) => row.selected && !row.blocked)
            .map((row) => ({ sheet: row.sheet, table: row.table.trim() })),
        );
      }
      return loadUpload(file, { sheet: loadSheet, table: target, mode });
    },
    onSuccess: (result) => {
      if ('results' in result) {
        const failed = result.results.filter((item) => item.error);
        // 部分成功要说清楚：哪些进来了、哪些没有、为什么。只报一句"导入完成"
        // 会让用户以为全成了。
        if (failed.length) {
          toast.error(
            `${summarizeOutcomes(result.results)}。${failed
              .map((item) => `「${item.table}」${item.error?.message ?? ''}`)
              .join('　')}`,
          );
        } else {
          toast.success(summarizeOutcomes(result.results));
        }
      } else {
        toast.success(`已写入 ${result.row_count} 行`);
      }
      reset();
      onDone();
    },
    onError: (issue) => toast.error(describeError(issue)),
  });

  const existingNames = existing.map((item) => item.table);
  const problems = planProblems(rows, existingNames);
  const ready =
    mode === 'create'
      ? canImportPlan(rows, existingNames)
      : canLoad({ file, sheet: loadSheet, table: target });
  const readable = previews.filter((item) => item.error === undefined);

  const rename = (sheet: string, value: string) =>
    setRows((current) =>
      current.map((row) => (row.sheet === sheet ? { ...row, table: value } : row)),
    );
  const toggle = (sheet: string) =>
    setRows((current) =>
      current.map((row) => (row.sheet === sheet ? { ...row, selected: !row.selected } : row)),
    );

  return (
    <Dialog
      open={open}
      title="上传表格"
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        <>
          <Button
            onClick={() => {
              reset();
              onClose();
            }}
          >
            取消
          </Button>
          <Button
            variant="primary"
            onClick={() => submit.mutate()}
            disabled={!ready || submit.isPending}
          >
            {submit.isPending ? '导入中…' : '导入'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="文件" hint={`只支持 ${ACCEPTED_SUFFIX}。`}>
          <input
            type="file"
            accept={ACCEPTED_SUFFIX}
            className="block w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm file:text-slate-700"
            onChange={(event) => {
              const picked = event.target.files?.[0] ?? null;
              if (!picked) return;
              if (!isAcceptedFile(picked.name)) {
                reset();
                setError(`只支持 ${ACCEPTED_SUFFIX} 文件。`);
                return;
              }
              pick.mutate(picked);
            }}
          />
        </Field>

        {pick.isPending && <Spinner />}
        {error && <ErrorBanner message={error} />}

        {previews.length > 0 && (
          <Field label="导入方式">
            <Select
              value={mode}
              onChange={(event) => setMode(event.target.value as UploadMode)}
            >
              {(['create', 'append', 'replace'] as const).map((item) => (
                <option key={item} value={item} disabled={item !== 'create' && !existing.length}>
                  {modeLabel(item)}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {previews.length > 0 && mode === 'create' && (
          <div className="space-y-2">
            <div className="text-xs text-slate-500">
              勾选要导入的工作表，一次可以导多张。表名是建模页和问数里找到它的依据。
            </div>
            {previews.map((preview) => {
              const row = rows.find((item) => item.sheet === preview.sheet);
              if (!row) return null;
              return (
                <div
                  key={preview.sheet}
                  className="rounded-md border border-slate-200 p-3"
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={row.selected}
                      disabled={row.blocked !== undefined}
                      onChange={() => toggle(preview.sheet)}
                    />
                    <span className="truncate text-sm text-slate-900">{preview.sheet}</span>
                    <span className="ml-auto shrink-0 text-xs text-slate-400">
                      {row.blocked
                        ? row.blocked
                        : `${preview.row_count} 行 · ${preview.columns?.length} 列`}
                    </span>
                  </div>

                  {row.selected && !row.blocked && (
                    <div className="mt-2 space-y-2 pl-6">
                      <Input
                        value={row.table}
                        onChange={(event) => rename(preview.sheet, event.target.value)}
                      />
                      {problems[preview.sheet] && (
                        <p className="text-xs text-rose-600">{problems[preview.sheet]}</p>
                      )}
                      <ul className="max-h-24 space-y-0.5 overflow-y-auto text-xs">
                        {(preview.columns ?? []).map((column) => (
                          <li key={column.name} className="flex justify-between gap-3">
                            <span className="truncate text-slate-600">{column.name}</span>
                            <span className="shrink-0 text-slate-400">{column.type}</span>
                          </li>
                        ))}
                      </ul>
                      {(preview.changes ?? []).length > 0 && (
                        <div className="rounded-md bg-amber-50 px-2 py-1.5 text-xs text-amber-700">
                          <div className="mb-0.5 font-medium">自动改过，导入前请确认：</div>
                          <ul>
                            {(preview.changes ?? []).map((change) => (
                              <li key={change}>· {change}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {previews.length > 0 && mode !== 'create' && (
          <>
            <Field label="用哪张工作表的数据" hint="灌数一次一张。">
              <Select value={loadSheet} onChange={(event) => setLoadSheet(event.target.value)}>
                <option value="" disabled>
                  请选择
                </option>
                {readable.map((item) => (
                  <option key={item.sheet} value={item.sheet}>
                    {item.sheet}（{item.row_count} 行）
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="写入哪张表" hint="列名和类型必须和这张表对得上。">
              <Select value={target} onChange={(event) => setTarget(event.target.value)}>
                <option value="" disabled>
                  请选择
                </option>
                {existing.map((item) => (
                  <option key={item.table} value={item.table}>
                    {item.table}（{item.row_count} 行）
                  </option>
                ))}
              </Select>
            </Field>
            {mode === 'replace' && (
              <p className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
                「{target}」现有的数据会被全部清空，再写入这张工作表的内容。
              </p>
            )}
          </>
        )}
      </div>
    </Dialog>
  );
}
