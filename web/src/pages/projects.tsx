import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Database, Plus, UserPlus } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate } from '@analytics/lib/router';
import {
  bindProjectDataSource,
  createProject,
  listDataSources,
  listProjects,
} from '@analytics/api/analytics';
import type { AnalyticsProject } from '@analytics/api/types';
import {
  Button,
  Dialog,
  Empty,
  Field,
  Input,
  Select,
  Spinner,
  useToast,
} from '@analytics/components/ui';
import { avatarGradientOf, avatarStripeOf } from '@analytics/lib/avatar-gradient';
import { DataSourcesDialog } from './data-sources';
import { canCreateProject, engineLabel } from './data-source-form';
import { ProjectAuthorizeDialog } from './project-authorize';
import { ProjectDataSourceDialog } from './project-data-source';
import { describeError, formatDateTime } from '@analytics/lib/labels';
import { EDITION, appPath } from '@analytics/api/edition';
import { projectGridTemplateColumns } from '@analytics/lib/layout';

export function projectStatusOf(project: AnalyticsProject): { label: string; tone: string } {
  if (project.active_release_id) return { label: '已发布', tone: 'text-emerald-600' };
  if (project.latest_revision_id) return { label: '建模中', tone: 'text-blue-600' };
  return { label: '待导入数据表', tone: 'text-slate-400' };
}

function ProjectCard({
  project,
  onOpen,
  onAuthorize,
  onPickDataSource,
}: {
  project: AnalyticsProject;
  onOpen: () => void;
  /** 仅嵌入版传入：开源独立版不提供多用户 RBAC，不渲染授权入口。 */
  onAuthorize?: () => void;
  /** 仅嵌入版传入：独立版只有一个由设置页配置的数据源，没有可挑的。 */
  onPickDataSource?: () => void;
}) {
  const status = projectStatusOf(project);
  const name = project.name ?? '';
  return (
    // 外层从 button 改为 div：授权是卡片内的第二个动作，按钮不能嵌套按钮。
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onOpen();
      }}
      className="group relative w-full cursor-pointer overflow-hidden rounded-xl border border-slate-200 bg-white text-left shadow-[0_1px_3px_rgba(15,23,42,0.05)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md"
    >
      {/* 卡片内的次要动作。按钮不能嵌套按钮，所以外层是 div + role=button，
          这里每个都要 stopPropagation，否则点它们会连带打开项目。 */}
      <div className="absolute right-2 top-2 z-10 flex items-center gap-1">
        {onPickDataSource && (
          <button
            type="button"
            title="数据源"
            onClick={(event) => {
              event.stopPropagation();
              onPickDataSource();
            }}
            className="rounded-md bg-white/85 p-1.5 text-slate-500 opacity-0 shadow-sm transition-opacity hover:text-slate-900 focus:opacity-100 group-hover:opacity-100"
          >
            <Database className="h-4 w-4" />
          </button>
        )}
        {onAuthorize && (
          <button
            type="button"
            title="授权"
            onClick={(event) => {
              event.stopPropagation();
              onAuthorize();
            }}
            className="rounded-md bg-white/85 p-1.5 text-slate-500 opacity-0 shadow-sm transition-opacity hover:text-slate-900 focus:opacity-100 group-hover:opacity-100"
          >
            <UserPlus className="h-4 w-4" />
          </button>
        )}
      </div>
      <div className="h-11 w-full" style={{ background: avatarStripeOf(name) }} />
      <div className="-mt-5 flex min-w-0 flex-col px-4 pb-4">
        <div className="flex min-w-0 items-start gap-3">
          <div
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[11px] border-2 border-white text-base font-semibold text-white shadow-md"
            style={{ background: avatarGradientOf(name) }}
          >
            {name.charAt(0)?.toUpperCase()}
          </div>
          <div className="flex min-w-0 flex-col pt-1.5">
            <div className="truncate text-[14.5px] font-semibold leading-snug text-slate-900">
              {name}
            </div>
            <div className={`mt-0.5 text-xs ${status.tone}`}>{status.label}</div>
          </div>
        </div>
        <div className="mt-3.5 border-t border-slate-100 pt-3 text-xs text-slate-400">
          创建于 {formatDateTime(project.created_at)}
        </div>
      </div>
    </div>
  );
}

export function ProjectsPage({ ready }: { ready: boolean }) {
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const projects = useQuery({ queryKey: ['projects'], queryFn: listProjects, enabled: ready });
  const [creating, setCreating] = useState(false);
  const [managingSources, setManagingSources] = useState(false);
  const [pickingSourceFor, setPickingSourceFor] = useState<AnalyticsProject | null>(null);
  const [authorizing, setAuthorizing] = useState<AnalyticsProject | null>(null);
  const [name, setName] = useState('');
  // 空字符串 = 还没做选择。**不预选**：预选上默认库的话，用户一路回车就又回到了
  // "不知道自己连的是哪个库"。
  const [newProjectSource, setNewProjectSource] = useState('');

  // 新建对话框里要列数据源，所以对话框一开就取。
  const dataSources = useQuery({
    queryKey: ['analytics-data-sources'],
    queryFn: listDataSources,
    enabled: creating && EDITION === 'embedded',
  });

  const create = useMutation({
    /**
     * 建项目 + 绑数据源是两次调用，核心没有"建的时候一起绑"的接口。
     *
     * 所以第二步失败要单独说：那时项目**已经建好了**，只是没绑上。静默跳走的话
     * 用户会带着一个悄悄连着默认库的项目往下走。
     */
    mutationFn: async () => {
      const project = await createProject(name.trim());
      try {
        await bindProjectDataSource(project.id, newProjectSource);
        return { project, bound: true };
      } catch {
        return { project, bound: false };
      }
    },
    onSuccess: ({ project, bound }) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCreating(false);
      setName('');
      setNewProjectSource('');
      if (!bound) {
        toast.error('项目已创建，但数据源没绑上。请在项目卡片上补绑后再导入表。');
      }
      navigate(appPath(`/projects/${project.id}`));
    },
    onError: (error) => toast.error(describeError(error)),
  });

  if (!ready) {
    // 两种部署的配置出口不同:独立版有自己的设置页(数据源 + 模型);
    // 嵌入商业版时数据源与模型都来自宿主的租户配置,这里没有设置页可去。
    return EDITION === 'embedded' ? (
      <Empty
        title="智能问数尚未就绪"
        hint="请联系管理员在系统模型配置中启用聊天模型与嵌入模型。"
      />
    ) : (
      <Empty
        title="服务尚未配置"
        hint="先在设置中连接 PostgreSQL 数据源并填写聊天模型与嵌入模型。"
        action={
          <Link to={appPath('/settings')}>
            <Button variant="primary">前往设置</Button>
          </Link>
        }
      />
    );
  }

  return (
    <div>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">项目</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            一个项目对应一套语义模型：导入表、建关系、AI 建模、发布，然后用自然语言提问。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {EDITION === 'embedded' && (
            <Button
              icon={<Database className="h-4 w-4" />}
              onClick={() => setManagingSources(true)}
            >
              数据源
            </Button>
          )}
          <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>
            新建项目
          </Button>
        </div>
      </div>
      {projects.isPending && <Spinner />}
      {projects.isError && (
        <div className="text-sm text-red-600">{describeError(projects.error)}</div>
      )}
      {projects.data && projects.data.items.length === 0 && (
        <Empty
          title="还没有项目"
          hint="新建一个项目，从数据库里挑几张业务表开始。"
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              新建项目
            </Button>
          }
        />
      )}
      {projects.data && projects.data.items.length > 0 && (
        <div
          className="grid gap-4"
          style={{ gridTemplateColumns: projectGridTemplateColumns() }}
        >
          {projects.data.items.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onOpen={() => navigate(appPath(`/projects/${project.id}`))}
              onAuthorize={
                EDITION === 'embedded'
                  ? () => setAuthorizing(project)
                  : undefined
              }
              onPickDataSource={
                EDITION === 'embedded'
                  ? () => setPickingSourceFor(project)
                  : undefined
              }
            />
          ))}
        </div>
      )}

      <DataSourcesDialog
        open={managingSources}
        onClose={() => setManagingSources(false)}
      />

      {pickingSourceFor && (
        <ProjectDataSourceDialog
          open
          projectId={pickingSourceFor.id}
          projectName={pickingSourceFor.name ?? ''}
          onClose={() => setPickingSourceFor(null)}
        />
      )}

      {authorizing && (
        <ProjectAuthorizeDialog
          open
          projectId={authorizing.id}
          projectName={authorizing.name ?? ''}
          onClose={() => setAuthorizing(null)}
        />
      )}

      <Dialog
        open={creating}
        title="新建项目"
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button onClick={() => setCreating(false)}>取消</Button>
            <Button
              variant="primary"
              loading={create.isPending}
              disabled={!canCreateProject({ name, dataSourceChoice: newProjectSource })}
              onClick={() => create.mutate()}
            >
              创建
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <Field label="项目名称" hint="例如：经营分析、销售看板">
            <Input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === 'Enter' &&
                  canCreateProject({ name, dataSourceChoice: newProjectSource })
                ) {
                  create.mutate();
                }
              }}
            />
          </Field>
          {EDITION === 'embedded' && (
            <Field
              label="数据源"
              hint="建模会从这个库里读表。建好模型之后再换库要重新核对，所以现在就定下来。"
            >
              <Select
                value={newProjectSource}
                onChange={(event) => setNewProjectSource(event.target.value)}
              >
                {/* 占位项禁用：必须做一个明确选择，"默认库"也是一种选择。 */}
                <option value="" disabled>
                  {(dataSources.data ?? []).length ? '请选择' : '还没有数据源'}
                </option>
                {(dataSources.data ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}（{engineLabel(item.engine)}）
                  </option>
                ))}
              </Select>
            </Field>
          )}
        </div>
      </Dialog>
    </div>
  );
}
