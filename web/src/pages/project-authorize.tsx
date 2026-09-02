import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import {
  grantProject,
  listProjectGrants,
  revokeProject,
  searchGrantSubjects,
  type GrantSubjectOption,
  type GrantSubjectType,
  type ProjectRole,
} from '@analytics/api/analytics';
import {
  Badge,
  Button,
  Dialog,
  Empty,
  ErrorBanner,
  Input,
  Select,
  Spinner,
} from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

const SUBJECT_TABS: ReadonlyArray<{ key: GrantSubjectType; label: string }> = [
  { key: 'user', label: '用户' },
  { key: 'org', label: '组织' },
  { key: 'group', label: '协作组' },
];

const ROLES: ReadonlyArray<{ key: ProjectRole; label: string; hint: string }> = [
  { key: 'viewer', label: '可提问', hint: '能用该项目的助手提问、查看结果' },
  { key: 'editor', label: '可建模', hint: '在可提问之上，还能修改语义模型' },
  { key: 'admin', label: '可管理', hint: '在可建模之上，还能管理该项目' },
];

/**
 * 问数项目授权：把用户/组织/协作组授权到一个项目（= 一套语义模型）。
 *
 * 项目是问数唯一的权限资源：其下实体与指标/维度纯继承、不单独授权。助手不是
 * 权限资源——分享助手只放大"谁能看见它"，能问到什么仍由本处的项目授权决定，
 * 所以分享只可能收窄、不可能放宽。
 *
 * 仅嵌入商业版渲染（调用方按 EDITION 判断）；开源独立版不提供多用户 RBAC。
 */
export function ProjectAuthorizeDialog({
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
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<GrantSubjectType>('user');
  const [keyword, setKeyword] = useState('');
  const [role, setRole] = useState<ProjectRole>('viewer');
  const [error, setError] = useState<string | null>(null);

  const grantsKey = ['analytics-project-grants', projectId];
  const grants = useQuery({
    queryKey: grantsKey,
    queryFn: () => listProjectGrants(projectId),
    enabled: open && Boolean(projectId),
  });
  const subjects = useQuery({
    queryKey: ['analytics-grant-subjects', kind, keyword],
    queryFn: () => searchGrantSubjects(kind, keyword),
    enabled: open,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: grantsKey });

  const add = useMutation({
    mutationFn: (subject: GrantSubjectOption) =>
      grantProject(projectId, {
        subject_type: kind,
        subject_id: subject.id,
        role_code: role,
      }),
    onSuccess: () => {
      setError(null);
      void refresh();
    },
    onError: (cause) => setError(describeError(cause)),
  });

  const remove = useMutation({
    mutationFn: (row: {
      subject_type: GrantSubjectType;
      subject_id: string;
      role_code: ProjectRole;
    }) => revokeProject(projectId, row),
    onSuccess: () => {
      setError(null);
      void refresh();
    },
    onError: (cause) => setError(describeError(cause)),
  });

  /** 已授权主体拍平成一张表：三类主体的字段名不同，行为完全一致。 */
  const grantedRows = useMemo(() => {
    const data = grants.data;
    if (!data) return [];
    return [
      ...(data.users ?? []).map((item) => ({
        subject_type: 'user' as const,
        subject_id: item.user_id,
        name: item.nickname || item.username || item.user_id,
        role_code: (item.role_code || 'viewer') as ProjectRole,
      })),
      ...(data.orgs ?? []).map((item) => ({
        subject_type: 'org' as const,
        subject_id: item.org_unit_id,
        name: item.org_name || item.name || item.org_unit_id,
        role_code: (item.role_code || 'viewer') as ProjectRole,
      })),
      ...(data.groups ?? []).map((item) => ({
        subject_type: 'group' as const,
        subject_id: item.group_id,
        name: item.group_name || item.name || item.group_id,
        role_code: (item.role_code || 'viewer') as ProjectRole,
      })),
    ];
  }, [grants.data]);

  const grantedIds = useMemo(
    () =>
      new Set(
        grantedRows
          .filter((row) => row.subject_type === kind)
          .map((row) => row.subject_id),
      ),
    [grantedRows, kind],
  );

  const busy = add.isPending || remove.isPending;

  return (
    <Dialog open={open} title={`授权「${projectName}」`} onClose={onClose} width="max-w-2xl">
      <div className="space-y-4">
        {error && <ErrorBanner message={error} />}

        <p className="rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-slate-600">
          授权的是<b>整个项目</b>（一套语义模型）。项目下的实体、指标与维度随项目继承，
          不单独授权。被授权的人能看到什么，与谁把助手分享给他无关。
        </p>

        <section className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1">
              {SUBJECT_TABS.map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => {
                    setKind(tab.key);
                    // 关键词跟着 tab 走：三类主体各查各的接口，把上一个 tab 的
                    // 搜索词带过来会让新 tab 直接空列表，看起来像"这里没有数据"。
                    setKeyword('');
                  }}
                  className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                    kind === tab.key
                      ? 'bg-blue-600 font-medium text-white'
                      : 'text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <Input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="搜索名称"
              className="h-8 w-44 text-xs"
            />
            <Select
              value={role}
              onChange={(event) => setRole(event.target.value as ProjectRole)}
              className="h-8 w-32 text-xs"
            >
              {ROLES.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </Select>
            <span className="text-[11px] text-slate-400">
              {ROLES.find((item) => item.key === role)?.hint}
            </span>
          </div>

          <div className="max-h-44 overflow-auto rounded-lg border border-slate-200">
            {subjects.isPending ? (
              <div className="p-3">
                <Spinner label="加载中" />
              </div>
            ) : subjects.data?.length ? (
              <ul className="divide-y divide-slate-100">
                {subjects.data.map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center justify-between px-3 py-1.5 text-xs"
                  >
                    <span className="text-slate-700">{item.name}</span>
                    {grantedIds.has(item.id) ? (
                      <span className="text-slate-400">已授权</span>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => add.mutate(item)}
                      >
                        授权
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="p-3">
                <Empty title="没有匹配的主体" />
              </div>
            )}
          </div>
        </section>

        <section className="space-y-2">
          <h3 className="text-xs font-medium text-slate-500">已授权</h3>
          {grants.isPending ? (
            <Spinner label="加载中" />
          ) : grantedRows.length ? (
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
              {grantedRows.map((row) => (
                <li
                  key={`${row.subject_type}:${row.subject_id}`}
                  className="flex items-center justify-between px-3 py-1.5 text-xs"
                >
                  <span className="flex items-center gap-2">
                    <Badge tone="slate">
                      {SUBJECT_TABS.find((tab) => tab.key === row.subject_type)?.label}
                    </Badge>
                    <span className="text-slate-700">{row.name}</span>
                    <span className="text-slate-400">
                      {ROLES.find((item) => item.key === row.role_code)?.label ??
                        row.role_code}
                    </span>
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => remove.mutate(row)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty
              title="还没有授权任何人"
              hint="只有项目创建者能访问；授权后其他人才能用该项目提问。"
            />
          )}
        </section>
      </div>
    </Dialog>
  );
}
