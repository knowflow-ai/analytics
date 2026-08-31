import { useMutation, useQuery } from '@tanstack/react-query';
import { ArrowLeft, Send, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, useParams } from '@analytics/lib/router';
import {
  getModelingSummary,
  getRelease,
  getRevision,
  newResourceId,
  previewQuery,
  query,
  versionOf,
  type QueryInput,
} from '@analytics/api/analytics';
import type { AnalyticsClarificationOption, AnalyticsDataset, AnalyticsQueryResponse } from '@analytics/api/types';
import {
  buildClarificationContinuation,
  QueryAnswer,
  type QueryTurn,
  type QueryTurnTarget,
} from '@analytics/components/query-answer';
import { Button, Empty, Select, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';
import { appPath } from '@analytics/api/edition';

const SUGGESTIONS = ['各地区的订单金额是多少？', '最近 30 天每天的订单数', '销售额最高的前 10 个客户'];

export function AskPage() {
  const { projectId = '' } = useParams();
  const toast = useToast();
  const summary = useQuery({ queryKey: ['summary', projectId], queryFn: () => getModelingSummary(projectId) });
  const releaseId = summary.data?.active_release_id ?? null;
  const draftId = summary.data?.revision_state === 'draft' || summary.data?.revision_state === 'validated'
    ? summary.data?.revision_id ?? null
    : null;
  const release = useQuery({
    queryKey: ['release', projectId, releaseId],
    queryFn: () => getRelease(projectId, releaseId!),
    enabled: Boolean(releaseId),
  });
  const draft = useQuery({
    queryKey: ['revision', projectId, draftId],
    queryFn: () => getRevision(projectId, draftId!),
    enabled: Boolean(draftId),
  });

  const [mode, setMode] = useState<'release' | 'draft'>('release');
  useEffect(() => {
    if (!releaseId && draftId) setMode('draft');
  }, [draftId, releaseId]);
  const datasets: AnalyticsDataset[] = useMemo(
    () => (mode === 'release' ? release.data?.release.datasets : draft.data?.semantic_spec.datasets) ?? [],
    [draft.data, mode, release.data],
  );
  // Result columns are semantic ids; show the governed names instead.
  const elementNames = useMemo(() => {
    const source = mode === 'release' ? release.data?.release : draft.data?.semantic_spec;
    const map = new Map<string, string>();
    source?.dimensions.forEach((d) => map.set(d.id, d.name));
    source?.metrics.forEach((m) => map.set(m.id, m.name));
    return map;
  }, [draft.data, mode, release.data]);
  const columnName = (id: string) => elementNames.get(id) ?? id.split(':').pop() ?? id;
  const currentTarget: QueryTurnTarget | null =
    mode === 'release'
      ? { mode: 'release' }
      : draft.data
        ? { mode: 'draft', revisionId: draft.data.id, version: versionOf(draft.data) }
        : null;
  const [question, setQuestion] = useState('');
  const [turns, setTurns] = useState<QueryTurn[]>([]);
  const conversationId = useRef(newResourceId('conv'));
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), [turns]);

  const ask = useMutation({
    mutationFn: async ({ turnId, input, target }: { turnId: string; input: QueryInput; target: QueryTurnTarget }) => {
      const response =
        target.mode === 'release'
          ? await query(projectId, input)
          : await previewQuery(projectId, target.revisionId, target.version, input);
      return { turnId, response };
    },
    onSuccess: ({ turnId, response }) =>
      setTurns((current) => current.map((t) => (t.id === turnId ? { ...t, response, pending: false } : t))),
    onError: (error, { turnId }) =>
      setTurns((current) =>
        current.map((t) => (t.id === turnId ? { ...t, error: describeError(error), pending: false } : t)),
      ),
  });

  const submit = (
    text: string,
    extra: Partial<QueryInput> = {},
    target: QueryTurnTarget | null = currentTarget,
  ) => {
    const trimmed = text.trim();
    if (!trimmed || !datasets.length || !target) return;
    const turnId = newResourceId('turn');
    const input: QueryInput = {
      question: trimmed,
      dataset_ids: datasets.map((d) => d.id),
      conversation_id: conversationId.current,
      ...extra,
    };
    setTurns((current) => [
      ...current,
      { id: turnId, question: trimmed, input, target, pending: true },
    ]);
    setQuestion('');
    ask.mutate({ turnId, input, target });
  };

  const choose = (option: AnalyticsClarificationOption, response: AnalyticsQueryResponse) => {
    const origin = turns.find((t) => t.response === response);
    if (!origin?.input || !origin.target) return;
    submit(
      origin.question,
      buildClarificationContinuation(origin.input, option, response),
      origin.target,
    );
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit(question);
  };

  if (summary.isPending) return <Spinner />;
  if (summary.isError) return <div className="text-sm text-red-600">{describeError(summary.error)}</div>;
  if (!releaseId && !draftId) {
    return (
      <Empty
        title="项目还没有可提问的模型"
        hint="先导入数据表并完成建模。"
        action={
          <Link to={appPath(`/projects/${projectId}`)}>
            <Button variant="primary">去建模</Button>
          </Link>
        }
      />
    );
  }
  const loadingScope = mode === 'release' ? release.isPending : draft.isPending;

  return (
    <div className="mx-auto flex h-[calc(100vh-7rem)] max-w-4xl flex-col">
      <div className="mb-3 flex items-center gap-3">
        <Link
          to={appPath(`/projects/${projectId}`)}
          className="grid h-8 w-8 place-items-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-700"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-semibold text-slate-900">{summary.data.project_name}</h1>
          <div className="text-[11px] text-slate-400">
            {mode === 'release' ? '对线上完整语义目录提问' : '对草稿完整语义目录预览提问（含诊断，不影响线上）'}
          </div>
        </div>
        {releaseId && draftId && (
          <div className="w-36 shrink-0">
            <Select value={mode} onChange={(e) => setMode(e.target.value as 'release' | 'draft')}>
              <option value="release">线上版本</option>
              <option value="draft">草稿预览</option>
            </Select>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto rounded-xl border border-slate-200 bg-white">
        {loadingScope && <Spinner />}
        {!loadingScope && datasets.length === 0 && (
          <Empty
            title="当前版本尚不可问数"
            hint="完整语义目录不会丢失；请在建模工作台运行 AI 建模并完成验证。"
          />
        )}
        {!loadingScope && datasets.length > 0 && turns.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
            <span className="grid h-12 w-12 place-items-center rounded-2xl bg-gradient-to-br from-blue-600 to-blue-400 text-white shadow-md">
              <Sparkles className="h-6 w-6" />
            </span>
            <div className="text-sm font-medium text-slate-700">用一句话提问，系统会在完整语义目录中匹配并翻译成 SQL</div>
            {datasets.length > 1 && (
              <div className="max-w-lg text-xs text-slate-400">
                系统会自动选择安全的业务分析路径；只有业务含义确实无法唯一判断时才会请你确认。
              </div>
            )}
            <div className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 hover:border-blue-300 hover:text-blue-600"
                  onClick={() => submit(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="flex flex-col gap-5 p-5">
          {turns.map((turn) => (
            <div key={turn.id} className="flex flex-col gap-2">
              <div className="self-end rounded-2xl rounded-br-sm bg-blue-600 px-3.5 py-2 text-[13px] text-white">
                {turn.question}
              </div>
              <div className="max-w-full self-start rounded-2xl rounded-bl-sm border border-slate-200 bg-slate-50 px-4 py-3">
                <QueryAnswer projectId={projectId} turn={turn} columnName={columnName} onChoose={choose} />
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>

      <form onSubmit={onSubmit} className="mt-3 flex items-center gap-2">
        <input
          className="h-11 flex-1 rounded-xl border border-slate-200 bg-white px-4 text-[13px] shadow-sm placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100"
          placeholder="例如：上个月各地区的销售额是多少？"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          disabled={datasets.length === 0}
        />
        <Button
          type="submit"
          variant="primary"
          className="h-11 px-4"
          icon={<Send className="h-4 w-4" />}
          disabled={!question.trim() || datasets.length === 0}
          onClick={() => {
            if (ask.isPending) toast.info('上一个问题还在处理中。');
          }}
        >
          提问
        </Button>
      </form>
    </div>
  );
}
