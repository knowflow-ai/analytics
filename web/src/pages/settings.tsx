import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, Database, MessageSquareText, Sparkles } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import {
  getOssSettings,
  saveOssSettings,
  testDatasource,
  testModel,
  type ModelEndpointSettings,
  type OssSettings,
} from '@analytics/api/oss';
import { Button, Card, Field, Input, Select, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

type Draft = Pick<OssSettings, 'datasource_database_url' | 'chat_model' | 'embedding_model'>;

const EMPTY_ENDPOINT: ModelEndpointSettings = {
  base_url: '',
  api_key: '',
  model: '',
  max_output_tokens: null,
  thinking: 'auto',
};

export const POSTGRES_CONNECTION_PLACEHOLDER =
  'postgresql+psycopg://user:password@host:5432/database';
export const POSTGRES_CONNECTION_HINT =
  '使用 psycopg 3；postgresql:// 与 postgres:// 会自动转换。Docker Compose 连接宿主机时请用 host.docker.internal，不要用 127.0.0.1。';

function Section({
  icon,
  title,
  description,
  configured,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  configured: boolean;
  children: ReactNode;
}) {
  return (
    <Card className="p-6">
      <div className="mb-5 flex items-start gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-slate-100 text-slate-600">
          {icon}
        </span>
        <div className="flex-1">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            {title}
            {configured && <CheckCircle2 className="h-4 w-4 text-emerald-500" />}
          </div>
          <div className="mt-0.5 text-xs text-slate-400">{description}</div>
        </div>
      </div>
      {children}
    </Card>
  );
}

function EndpointFields({
  value,
  onChange,
  placeholderModel,
  generation = false,
}: {
  value: ModelEndpointSettings;
  onChange: (next: ModelEndpointSettings) => void;
  placeholderModel: string;
  /** Output budget and thinking mode only apply to the chat model. */
  generation?: boolean;
}) {
  return (
    <div className="grid gap-4 grid-cols-[repeat(auto-fit,minmax(260px,1fr))]">
      <Field label="Base URL" hint="OpenAI 兼容接口地址，以 /v1 结尾">
        <Input
          placeholder="https://api.openai.com/v1"
          value={value.base_url}
          onChange={(event) => onChange({ ...value, base_url: event.target.value })}
        />
      </Field>
      <Field label="API Key" hint="留空表示无需鉴权（如本地 Ollama / vLLM）">
        <Input
          type="password"
          autoComplete="off"
          value={value.api_key}
          onChange={(event) => onChange({ ...value, api_key: event.target.value })}
        />
      </Field>
      <Field label="模型名称">
        <Input
          placeholder={placeholderModel}
          value={value.model}
          onChange={(event) => onChange({ ...value, model: event.target.value })}
        />
      </Field>
      {generation && (
        <>
          <Field label="最大输出长度" hint="留空使用内置上限 16384。接口不声明模型能力，需按部署实际填写。">
            <Input
              type="number"
              min={256}
              max={131072}
              placeholder="16384"
              value={value.max_output_tokens ?? ''}
              onChange={(event) =>
                onChange({
                  ...value,
                  max_output_tokens: event.target.value ? Number(event.target.value) : null,
                })
              }
            />
          </Field>
          <Field
            label="思考模式"
            hint="思考与答案共用同一份输出预算。模型会思考却预算不足时，会写不出答案。"
          >
            <Select
              value={value.thinking}
              onChange={(event) =>
                onChange({ ...value, thinking: event.target.value as 'auto' | 'off' })
              }
            >
              <option value="auto">跟随模型默认</option>
              <option value="off">关闭思考</option>
            </Select>
          </Field>
        </>
      )}
    </div>
  );
}

export function SettingsPage() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ['oss-settings'], queryFn: getOssSettings });
  const [draft, setDraft] = useState<Draft>({
    datasource_database_url: '',
    chat_model: EMPTY_ENDPOINT,
    embedding_model: EMPTY_ENDPOINT,
  });
  useEffect(() => {
    if (settings.data) {
      setDraft({
        datasource_database_url: settings.data.datasource_database_url,
        chat_model: settings.data.chat_model,
        embedding_model: settings.data.embedding_model,
      });
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => saveOssSettings(draft),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['oss-status'] });
      queryClient.setQueryData(['oss-settings'], result);
      if (result.ready) toast.success('设置已保存，服务已就绪。');
      else toast.error(`设置已保存，但服务未就绪：${result.error ?? '配置不完整'}`);
    },
    onError: (error) => toast.error(describeError(error)),
  });
  const probeDatasource = useMutation({
    mutationFn: () => testDatasource(draft.datasource_database_url),
    onSuccess: () => toast.success('数据库连接成功。'),
    onError: (error) => toast.error(`连接失败：${describeError(error)}`),
  });
  const probeChat = useMutation({
    mutationFn: () => testModel('chat_model', draft.chat_model),
    onSuccess: (result) => toast.success(`聊天模型 ${result.model} 响应正常。`),
    onError: (error) => toast.error(`聊天模型测试失败：${describeError(error)}`),
  });
  const probeEmbedding = useMutation({
    mutationFn: () => testModel('embedding_model', draft.embedding_model),
    onSuccess: (result) => toast.success(`嵌入模型正常，向量维度 ${result.dimension}。`),
    onError: (error) => toast.error(`嵌入模型测试失败：${describeError(error)}`),
  });

  if (settings.isPending) return <Spinner />;
  if (settings.isError) {
    return <div className="text-sm text-red-600">{describeError(settings.error)}</div>;
  }
  const configured = settings.data.configured;

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-slate-900">设置</h1>
        <p className="mt-1 text-xs text-slate-400">
          连接一个 PostgreSQL 业务库，并指定聊天模型与嵌入模型。保存后立即生效，无需重启。
        </p>
      </div>
      <div className="flex flex-col gap-5">
        <Section
          icon={<Database className="h-4 w-4" />}
          title="数据源"
          description="只读访问即可。语义模型存放在服务自己的 catalog 库，不会写入这里。"
          configured={configured.datasource}
        >
          <Field label="PostgreSQL 连接串" hint={POSTGRES_CONNECTION_HINT}>
            <Input
              placeholder={POSTGRES_CONNECTION_PLACEHOLDER}
              value={draft.datasource_database_url}
              onChange={(event) =>
                setDraft({ ...draft, datasource_database_url: event.target.value })
              }
            />
          </Field>
          <div className="mt-3">
            <Button
              size="sm"
              loading={probeDatasource.isPending}
              disabled={!draft.datasource_database_url}
              onClick={() => probeDatasource.mutate()}
            >
              测试连接
            </Button>
          </div>
        </Section>

        <Section
          icon={<MessageSquareText className="h-4 w-4" />}
          title="聊天模型"
          description="用于 AI 建模与自然语言解析。建议使用 120B 级以上、支持 JSON 输出的模型。"
          configured={configured.chat_model}
        >
          <EndpointFields
            value={draft.chat_model}
            generation
            onChange={(chat_model) => setDraft({ ...draft, chat_model })}
            placeholderModel="gpt-4.1 / qwen3-235b / deepseek-v3"
          />
          <div className="mt-3">
            <Button
              size="sm"
              loading={probeChat.isPending}
              disabled={!draft.chat_model.base_url || !draft.chat_model.model}
              onClick={() => probeChat.mutate()}
            >
              测试模型
            </Button>
          </div>
        </Section>

        <Section
          icon={<Sparkles className="h-4 w-4" />}
          title="嵌入模型"
          description="用于问数时的语义匹配索引。发布后更换嵌入模型需要重新发布。"
          configured={configured.embedding_model}
        >
          <EndpointFields
            value={draft.embedding_model}
            onChange={(embedding_model) => setDraft({ ...draft, embedding_model })}
            placeholderModel="text-embedding-3-small / bge-m3"
          />
          <div className="mt-3">
            <Button
              size="sm"
              loading={probeEmbedding.isPending}
              disabled={!draft.embedding_model.base_url || !draft.embedding_model.model}
              onClick={() => probeEmbedding.mutate()}
            >
              测试模型
            </Button>
          </div>
        </Section>

        <div className="flex justify-end">
          <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
            保存设置
          </Button>
        </div>
      </div>
    </div>
  );
}
