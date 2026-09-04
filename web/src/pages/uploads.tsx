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
  canCreate,
  canLoad,
  defaultTableName,
  isAcceptedFile,
  modeLabel,
  tableNameProblem,
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

/** 选文件 → 选 sheet → 看确认表 → 落库。 */
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
  const [sheets, setSheets] = useState<string[]>([]);
  const [sheet, setSheet] = useState('');
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [mode, setMode] = useState<UploadMode>('create');
  const [table, setTable] = useState('');
  const [error, setError] = useState('');

  const reset = () => {
    setFile(null);
    setSheets([]);
    setSheet('');
    setPreview(null);
    setMode('create');
    setTable('');
    setError('');
  };

  const pick = useMutation({
    mutationFn: async (picked: File) => {
      const first = await inspectUpload(picked);
      // 只有一张表时直接看它——多一次点击换不来任何信息。
      const only = first.sheets.length === 1 ? first.sheets[0] : '';
      const detail = only ? await inspectUpload(picked, only) : null;
      return { picked, sheets: first.sheets, sheet: only, preview: detail?.preview ?? null };
    },
    onSuccess: (result) => {
      setFile(result.picked);
      setSheets(result.sheets);
      setSheet(result.sheet);
      setPreview(result.preview);
      setTable(defaultTableName(result.picked.name));
      setError('');
    },
    onError: (issue) => {
      reset();
      setError(describeError(issue));
    },
  });

  const chooseSheet = useMutation({
    mutationFn: async (name: string) => {
      if (!file) throw new Error('还没有选文件');
      return inspectUpload(file, name);
    },
    onSuccess: (result) => {
      setSheet(result.preview?.sheet ?? '');
      setPreview(result.preview);
      setError('');
    },
    onError: (issue) => {
      setPreview(null);
      setError(describeError(issue));
    },
  });

  const submit = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error('还没有选文件');
      if (mode === 'create') return commitUpload(file, { sheet, table: table.trim() });
      return loadUpload(file, { sheet, table, mode });
    },
    onSuccess: (result) => {
      toast.success(
        mode === 'create'
          ? `已建表「${result.table}」，写入 ${result.row_count} 行`
          : `已写入 ${result.row_count} 行`,
      );
      reset();
      onDone();
    },
    onError: (issue) => toast.error(describeError(issue)),
  });

  const nameProblem =
    mode === 'create' ? tableNameProblem(table, existing.map((item) => item.table)) : '';
  const ready =
    mode === 'create'
      ? canCreate({ file, sheet, table, existing: existing.map((item) => item.table) })
      : canLoad({ file, sheet, table });

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

        {sheets.length > 1 && (
          <Field label="工作表" hint="一次导入一张。">
            <Select
              value={sheet}
              onChange={(event) => chooseSheet.mutate(event.target.value)}
            >
              <option value="" disabled>
                请选择
              </option>
              {sheets.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </Field>
        )}

        {preview && (
          <>
            <Field label="导入方式">
              <Select
                value={mode}
                onChange={(event) => {
                  const next = event.target.value as UploadMode;
                  setMode(next);
                  setTable(
                    next === 'create'
                      ? defaultTableName(file?.name ?? '')
                      : (existing[0]?.table ?? ''),
                  );
                }}
              >
                {(['create', 'append', 'replace'] as const).map((item) => (
                  <option key={item} value={item} disabled={item !== 'create' && !existing.length}>
                    {modeLabel(item)}
                  </option>
                ))}
              </Select>
            </Field>

            {mode === 'create' ? (
              <Field
                label="表名"
                hint={nameProblem || '建模页和问数里按这个名字找到它。'}
              >
                <Input value={table} onChange={(event) => setTable(event.target.value)} />
              </Field>
            ) : (
              <Field label="写入哪张表" hint="列名和类型必须和这张表对得上。">
                <Select value={table} onChange={(event) => setTable(event.target.value)}>
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
            )}

            <div className="rounded-md border border-slate-200 p-3">
              <div className="mb-2 text-xs text-slate-500">
                {preview.row_count} 行 · {preview.columns.length} 列
              </div>
              <ul className="max-h-40 space-y-1 overflow-y-auto text-xs">
                {preview.columns.map((column) => (
                  <li key={column.name} className="flex justify-between gap-3">
                    <span className="truncate text-slate-700">{column.name}</span>
                    <span className="shrink-0 text-slate-400">{column.type}</span>
                  </li>
                ))}
              </ul>
            </div>

            {preview.changes.length > 0 && (
              <div className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-700">
                <div className="mb-1 font-medium">这些地方自动改过，导入前请确认：</div>
                <ul className="space-y-0.5">
                  {preview.changes.map((change) => (
                    <li key={change}>· {change}</li>
                  ))}
                </ul>
              </div>
            )}

            {mode === 'replace' && (
              <p className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700">
                「{table}」现有的数据会被全部清空，再写入这个文件的内容。
              </p>
            )}
          </>
        )}
      </div>
    </Dialog>
  );
}
