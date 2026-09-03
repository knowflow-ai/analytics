import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  bindProjectDataSource,
  getProjectDataSource,
  listDataSources,
  unbindProjectDataSource,
} from '@analytics/api/analytics';
import {
  Button,
  Dialog,
  ErrorBanner,
  Field,
  Select,
  Spinner,
  useToast,
} from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import {
  FALLBACK_DATA_SOURCE as FALLBACK,
  dataSourceBindingAction,
  engineLabel,
  warnsAboutSemanticDrift,
} from './data-source-form';

/**
 * 给一个项目挑数据源。
 *
 * 不绑也能用：那种项目回落到部署配置的默认库——**存量项目全是这个状态**，所以
 * 「未绑定」是常态而不是错误，界面上不能显示成待办或告警。
 *
 * 换数据源是件重的事：语义模型是按原来那个库的表结构建的，换过去表未必存在、
 * 类型未必一样。所以换之前说清楚，而不是换完等用户在问数那头撞见。
 */

export function ProjectDataSourceDialog({
  open,
  projectId,
  projectName,
  onClose,
}: {
  open: boolean;
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string>(FALLBACK);

  const bound = useQuery({
    queryKey: ['analytics-project-data-source', projectId],
    queryFn: () => getProjectDataSource(projectId),
    enabled: open,
  });
  const sources = useQuery({
    queryKey: ['analytics-data-sources'],
    queryFn: listDataSources,
    enabled: open,
  });

  // 打开时把当前绑定回填进下拉框。不回填的话，用户点开只想看一眼、直接点保存，
  // 就把项目从"绑着 A"改成了"未绑定"。
  useEffect(() => {
    if (!open) return;
    setSelected(bound.data ? bound.data.id : FALLBACK);
  }, [open, bound.data]);

  const boundId = bound.data?.id ?? null;
  const action = dataSourceBindingAction({ boundId, selected });
  const changed = action !== 'none';
  const loading = bound.isPending || sources.isPending;

  const save = useMutation({
    mutationFn: () =>
      action === 'unbind'
        ? unbindProjectDataSource(projectId)
        : bindProjectDataSource(projectId, selected),
    onSuccess: () => {
      toast.success('数据源已更新');
      queryClient.invalidateQueries({
        queryKey: ['analytics-project-data-source', projectId],
      });
      onClose();
    },
    onError: (error) => toast.error(describeError(error)),
  });

  return (
    <Dialog
      open={open}
      title={`数据源 · ${projectName}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={!changed || save.isPending || loading}
          >
            {save.isPending ? '保存中…' : '保存'}
          </Button>
        </>
      }
    >
      {loading && <Spinner />}
      {bound.isError && <ErrorBanner message={describeError(bound.error)} />}
      {sources.isError && <ErrorBanner message={describeError(sources.error)} />}

      {!loading && (
        <div className="space-y-3">
          <Field
            label="连接到"
            hint="不选则使用部署配置的默认库。"
          >
            <Select
              value={selected}
              onChange={(event) => setSelected(event.target.value)}
            >
              <option value={FALLBACK}>默认库（部署配置）</option>
              {(sources.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}（{engineLabel(item.engine)}）
                </option>
              ))}
            </Select>
          </Field>

          {warnsAboutSemanticDrift({ boundId, selected }) && (
            <p className="rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-700">
              换库之后，这个项目已经建好的语义模型要按新库的表结构重新核对：表可能
              不存在、列类型可能不同。建议换完先跑一次发布前质量报告。
            </p>
          )}
          {(sources.data ?? []).length === 0 && (
            <p className="text-xs text-slate-400">
              还没有可选的数据源。需要管理员先在「数据源」里新建一个。
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}
