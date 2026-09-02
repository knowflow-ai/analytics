import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  fetchDataScope,
  fetchDataScopeOptions,
  saveDataScope,
  type DataScopeRowFilter,
  type GrantSubjectType,
} from '@analytics/api/analytics';
import { Button, Empty, ErrorBanner, Input, Select, Spinner } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

/**
 * 某个已授权主体的数据范围：看得到哪些实体、哪些行。
 *
 * 与授权分两层——授权决定"能不能进这个项目"，这里决定"进来之后能看到什么"。
 * 两项都不填 = 不收窄（该主体能看到项目的全部实体与全部行）。
 *
 * 权限的判定与执行都在服务端：这个界面只是配置入口，显隐不构成安全边界。
 */
export function ProjectDataScopePanel({
  projectId,
  subject,
  subjectName,
  onClose,
}: {
  projectId: string;
  subject: { subject_type: GrantSubjectType; subject_id: string };
  subjectName: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [models, setModels] = useState<string[]>([]);
  const [filters, setFilters] = useState<DataScopeRowFilter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const options = useQuery({
    queryKey: ['analytics-data-scope-options', projectId],
    queryFn: () => fetchDataScopeOptions(projectId),
  });
  const current = useQuery({
    queryKey: ['analytics-data-scope', projectId, subject.subject_type, subject.subject_id],
    queryFn: () => fetchDataScope(projectId, subject),
  });

  // 已有配置到达后填入表单一次；之后由用户编辑接管。
  useEffect(() => {
    if (!current.data) return;
    setModels(current.data.visible_model_ids);
    setFilters(current.data.row_filters);
  }, [current.data]);

  const save = useMutation({
    mutationFn: () =>
      saveDataScope(projectId, subject, {
        visible_model_ids: models,
        // 没填值的规则不提交：一条"等于空"的行权限会把人挡在所有行之外，
        // 那不是配置者的本意，而是一条没填完的规则。
        row_filters: filters.filter((item) => item.dimension_id && item.value.trim()),
      }),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ['analytics-data-scope'] });
      onClose();
    },
    onError: (cause) => setError(describeError(cause)),
  });

  const toggleModel = (id: string) =>
    setModels((previous) =>
      previous.includes(id)
        ? previous.filter((item) => item !== id)
        : [...previous, id],
    );

  const visibleDimensions = (options.data?.dimensions ?? []).filter(
    (item) => models.length === 0 || models.includes(item.model_id),
  );

  if (options.isPending || current.isPending) {
    return <Spinner label="加载中" />;
  }

  if (!options.data?.models.length) {
    return (
      <Empty
        title="该项目还没有已发布的语义模型"
        hint="数据范围绑定已发布版本；先发布一次再来配置。"
      />
    );
  }

  return (
    <div className="space-y-4">
      {error && <ErrorBanner message={error} />}

      <p className="rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs text-slate-600">
        正在为<b>{subjectName}</b>配置数据范围。两项都不填 = 不收窄，能看到该项目的全部内容。
      </p>

      <section className="space-y-2">
        <h3 className="text-xs font-medium text-slate-500">
          可见实体（不勾 = 全部可见）
        </h3>
        <div className="max-h-40 overflow-auto rounded-lg border border-slate-200">
          <ul className="divide-y divide-slate-100">
            {options.data.models.map((item) => (
              <li key={item.id} className="px-3 py-1.5 text-xs">
                <label className="flex cursor-pointer items-center gap-2">
                  <input
                    type="checkbox"
                    checked={models.includes(item.id)}
                    onChange={() => toggleModel(item.id)}
                  />
                  <span className="text-slate-700">{item.name}</span>
                </label>
              </li>
            ))}
          </ul>
        </div>
        <p className="text-[11px] text-slate-400">
          勾选后，该主体只能问到这些实体下的指标与维度；其它实体的名字也不会出现在联想与澄清里。
        </p>
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-medium text-slate-500">行过滤（不填 = 全部行）</h3>
          <Button
            size="sm"
            variant="ghost"
            disabled={!visibleDimensions.length}
            onClick={() =>
              setFilters((previous) => [
                ...previous,
                { dimension_id: visibleDimensions[0]?.id ?? '', operator: 'eq', value: '' },
              ])
            }
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            添加
          </Button>
        </div>
        {filters.length ? (
          <ul className="space-y-1.5">
            {filters.map((item, index) => (
              <li key={index} className="flex items-center gap-2">
                <Select
                  value={item.dimension_id}
                  onChange={(event) =>
                    setFilters((previous) =>
                      previous.map((row, position) =>
                        position === index
                          ? { ...row, dimension_id: event.target.value }
                          : row,
                      ),
                    )
                  }
                  className="h-8 flex-1 text-xs"
                >
                  {visibleDimensions.map((dimension) => (
                    <option key={dimension.id} value={dimension.id}>
                      {dimension.name}
                    </option>
                  ))}
                </Select>
                <span className="text-xs text-slate-400">=</span>
                <Input
                  value={item.value}
                  onChange={(event) =>
                    setFilters((previous) =>
                      previous.map((row, position) =>
                        position === index ? { ...row, value: event.target.value } : row,
                      ),
                    )
                  }
                  placeholder="值，如 华东"
                  className="h-8 flex-1 text-xs"
                />
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    setFilters((previous) =>
                      previous.filter((_row, position) => position !== index),
                    )
                  }
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-slate-400">
            未设置行过滤，该主体能看到所有行。多条规则之间是「或」。
          </p>
        )}
      </section>

      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onClose}>
          取消
        </Button>
        <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? '保存中' : '保存'}
        </Button>
      </div>
    </div>
  );
}
