import { useMutation } from '@tanstack/react-query';
import { Sparkles } from 'lucide-react';
import { suggestAliases } from '@analytics/api/analytics';
import { useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

/**
 * 别名输入框旁的「建议」按钮。别名覆盖度是问数错答的主因(实测:剥掉别名
 * 命中率 74.9%→56.1%),入口必须在手边。建议只预填,人工删改后才随资源保存。
 */
export function AliasSuggestButton({
  projectId,
  revisionId,
  revisionEtag,
  resourceType,
  modelId,
  name,
  bizName,
  description,
  currentAliases,
  onSuggest,
}: {
  projectId: string;
  revisionId: string;
  revisionEtag: number;
  resourceType: 'dimension' | 'metric';
  modelId: string;
  name: string;
  bizName: string;
  description: string;
  /** 当前输入框内容(顿号分隔),建议会追加去重而不是覆盖。 */
  currentAliases: string;
  onSuggest: (merged: string) => void;
}) {
  const toast = useToast();
  const run = useMutation({
    mutationFn: () =>
      suggestAliases(projectId, revisionId, revisionEtag, {
        resource_type: resourceType,
        model_id: modelId,
        name,
        biz_name: bizName,
        description,
        existing_aliases: currentAliases.split(/[，,、]/).map((s) => s.trim()).filter(Boolean),
      }),
    onSuccess: ({ aliases }) => {
      const existing = currentAliases.split(/[，,、]/).map((s) => s.trim()).filter(Boolean);
      const seen = new Set(existing);
      const merged = [...existing, ...aliases.filter((alias) => !seen.has(alias))];
      onSuggest(merged.join('，'));
    },
    onError: (error) => toast.error(describeError(error)),
  });
  return (
    <button
      type="button"
      disabled={run.isPending}
      onClick={() => run.mutate()}
      className="flex shrink-0 items-center gap-1 rounded-md border border-slate-200 px-2 text-[11px] text-slate-500 hover:border-slate-300 hover:text-blue-600 disabled:opacity-50"
      title="按名称与说明生成候选别名,可删改"
    >
      <Sparkles className="h-3 w-3" /> {run.isPending ? '生成中…' : '建议'}
    </button>
  );
}
