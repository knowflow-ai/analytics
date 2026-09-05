import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';

import {
  createDataSource,
  deleteDataSource,
  listDataSources,
  testDataSource,
  updateDataSource,
  type DataSource,
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
  ENGINES,
  canSaveDataSource,
  dsnPlaceholder,
  engineLabel,
} from './data-source-form';

/**
 * 数据源管理。
 *
 * 一个数据源 = 一套数据库连接。项目绑定它之后，扫表、画像、发布、问数全部打到那个
 * 库，并按它的引擎选方言。
 *
 * **连接串只进不出。** 填写时送给服务端加密入库，之后任何接口都不会再把它交出来
 * ——所以「修改」只能整条覆盖，不能回显后编辑。这不是省事，是不让凭据有第二次
 * 离开服务端的机会。
 *
 * 增删改要全局 admin（宿主 BFF 判定）；这里不自己判身份，被拒了就把服务端的话
 * 显示出来。前端判身份等于把权限逻辑抄成两份，迟早对不上。
 */

export function DataSourcesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<DataSource | null>(null);

  const sources = useQuery({
    queryKey: ['analytics-data-sources'],
    queryFn: listDataSources,
    enabled: open,
  });

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ['analytics-data-sources'] });

  const remove = useMutation({
    mutationFn: (item: DataSource) => deleteDataSource(item.id),
    onSuccess: () => {
      setRemoving(null);
      toast.success('数据源已删除');
      refresh();
    },
    // 还有项目在用时服务端会拒（DATA_SOURCE_IN_USE），那条消息比"删除失败"有用。
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <>
      <Dialog
        open={open}
        title="数据库连接"
        onClose={onClose}
        footer={<Button onClick={onClose}>关闭</Button>}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          {/* min-w-0：让说明文字承担收缩，而不是把按钮挤变形。 */}
          <p className="min-w-0 text-xs leading-relaxed text-slate-400">
            项目绑定数据源后，建模与问数都打到那个库。连接信息加密保存，保存后不再展示。
          </p>
          <Button
            variant="primary"
            icon={<Plus className="h-4 w-4" />}
            onClick={() => setCreating(true)}
          >
            新建
          </Button>
        </div>

        {sources.isPending && <Spinner />}
        {sources.isError && <ErrorBanner message={describeError(sources.error)} />}
        {sources.data && sources.data.length === 0 && (
          <Empty title="还没有数据源" hint="新建一个连接，项目才能绑上去。" />
        )}
        {sources.data && sources.data.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {sources.data.map((item) => (
              <li key={item.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <div className="truncate text-sm text-slate-900">{item.name}</div>
                  <div className="mt-0.5 text-xs text-slate-400">{engineLabel(item.engine)}</div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <RotateSecretButton dataSource={item} onDone={refresh} />
                  <Button
                    icon={<Trash2 className="h-4 w-4" />}
                    onClick={() => setRemoving(item)}
                  >
                    删除
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Dialog>

      <DataSourceFormDialog
        open={creating}
        onClose={() => setCreating(false)}
        onSaved={() => {
          setCreating(false);
          refresh();
        }}
      />

      {removing && (
        <ConfirmationDialog
          open
          title="删除数据源"
          description={`删除「${removing.name}」？还有项目在用时不会删成功。`}
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

/** 换连接串。连接串取不回来，所以只能整条覆盖，不做回显编辑。 */
function RotateSecretButton({
  dataSource,
  onDone,
}: {
  dataSource: DataSource;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button onClick={() => setOpen(true)}>修改</Button>
      <DataSourceFormDialog
        open={open}
        dataSource={dataSource}
        onClose={() => setOpen(false)}
        onSaved={() => {
          setOpen(false);
          onDone();
        }}
      />
    </>
  );
}

function DataSourceFormDialog({
  open,
  dataSource,
  onClose,
  onSaved,
}: {
  open: boolean;
  dataSource?: DataSource;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const editing = dataSource !== undefined;
  const [name, setName] = useState(dataSource?.name ?? '');
  // 引擎不可改：语义模型是按那个引擎的表结构建的，换引擎等于换了一套物理世界，
  // 已发布的模型、冻结的路由、确认记忆全部对不上。要换只能新建。
  const [engine, setEngine] = useState(dataSource?.engine ?? ENGINES[0].value);
  const [dsn, setDsn] = useState('');

  const probe = useMutation({
    mutationFn: () => testDataSource({ engine, dsn: dsn.trim() }),
    onSuccess: () => toast.success('连接成功'),
    onError: (error) => toast.error(describeError(error)),
  });

  const save = useMutation({
    mutationFn: () =>
      editing
        ? updateDataSource(dataSource.id, {
            name: name.trim(),
            dsn: dsn.trim() || undefined,
          })
        : createDataSource({ name: name.trim(), engine, dsn: dsn.trim() }),
    onSuccess: () => {
      toast.success(editing ? '数据源已更新' : '数据源已创建');
      setDsn('');
      onSaved();
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const canSave = canSaveDataSource({ name, dsn, editing });

  return (
    <Dialog
      open={open}
      title={editing ? '修改数据源' : '新建数据源'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            onClick={() => probe.mutate()}
            disabled={!dsn.trim() || probe.isPending}
          >
            {probe.isPending ? '连接中…' : '测试连接'}
          </Button>
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={!canSave || save.isPending}
          >
            {save.isPending ? '保存中…' : '保存'}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="名称">
          <Input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field
          label="数据库类型"
          hint={editing ? '不可修改：语义模型是按这个引擎的表结构建的。' : undefined}
        >
          <Select
            value={engine}
            disabled={editing}
            onChange={(event) => setEngine(event.target.value)}
          >
            {ENGINES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="连接串"
          hint={
            editing
              ? '留空则不改。连接信息保存后不再展示，所以只能整条替换。'
              : '保存时会先连一次，连不上不会存下来。'
          }
        >
          <Input
            value={dsn}
            placeholder={dsnPlaceholder(engine)}
            onChange={(event) => setDsn(event.target.value)}
          />
        </Field>
      </div>
    </Dialog>
  );
}
