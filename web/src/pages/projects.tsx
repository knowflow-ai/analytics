import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate } from '@analytics/lib/router';
import { createProject, listProjects } from '@analytics/api/analytics';
import type { AnalyticsProject } from '@analytics/api/types';
import { Button, Dialog, Empty, Field, Input, Spinner, useToast } from '@analytics/components/ui';
import { avatarGradientOf, avatarStripeOf } from '@analytics/lib/avatar-gradient';
import { describeError, formatDateTime } from '@analytics/lib/labels';
import { EDITION, appPath } from '@analytics/api/edition';
import { projectGridTemplateColumns } from '@analytics/lib/layout';

export function projectStatusOf(project: AnalyticsProject): { label: string; tone: string } {
  if (project.active_release_id) return { label: '已发布', tone: 'text-emerald-600' };
  if (project.latest_revision_id) return { label: '建模中', tone: 'text-blue-600' };
  return { label: '待导入数据表', tone: 'text-slate-400' };
}

function ProjectCard({ project, onOpen }: { project: AnalyticsProject; onOpen: () => void }) {
  const status = projectStatusOf(project);
  const name = project.name ?? '';
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group w-full overflow-hidden rounded-xl border border-slate-200 bg-white text-left shadow-[0_1px_3px_rgba(15,23,42,0.05)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md"
    >
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
    </button>
  );
}

export function ProjectsPage({ ready }: { ready: boolean }) {
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const projects = useQuery({ queryKey: ['projects'], queryFn: listProjects, enabled: ready });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const create = useMutation({
    mutationFn: () => createProject(name.trim()),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCreating(false);
      setName('');
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
        <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>
          新建项目
        </Button>
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
            />
          ))}
        </div>
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
              disabled={!name.trim()}
              onClick={() => create.mutate()}
            >
              创建
            </Button>
          </>
        }
      >
        <Field label="项目名称" hint="例如：经营分析、销售看板">
          <Input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && name.trim()) create.mutate();
            }}
          />
        </Field>
      </Dialog>
    </div>
  );
}
