import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { applyDictionaryPreview, generateDictionaryPreview, versionOf } from '@analytics/api/analytics';
import type {
  AnalyticsDictionaryCandidate,
  AnalyticsDictionaryPreview,
  AnalyticsRevision,
} from '@analytics/api/types';
import { Button, Input, Spinner, useToast } from '@analytics/components/ui';
import { describeError } from '@analytics/lib/labels';

/**
 * 维度值字典,内嵌在维度编辑器里:编辑维度时正是想知道「它有哪些取值、
 * 别名够不够」的时刻。
 *
 * 采集是确定性的(读真实数据),人工负责显示名与别名——「北上广」映射到
 * 三个城市值只有人知道。敏感维度不会自动采集,eligibility 会说明原因。
 */

interface Draft {
  displayName: string;
  aliases: string;
  accept: boolean;
}

export function DimensionDictionarySection({
  projectId,
  revision,
  dimensionId,
  acceptRevision,
  readOnly,
}: {
  projectId: string;
  revision: AnalyticsRevision;
  dimensionId: string;
  acceptRevision: (next: AnalyticsRevision) => void;
  readOnly: boolean;
}) {
  const toast = useToast();
  const [preview, setPreview] = useState<AnalyticsDictionaryPreview | null>(null);
  const [drafts, setDrafts] = useState<Map<string, Draft>>(new Map());
  const existing = revision.semantic_catalog.dimensionValues.filter(
    (item) => item.dimension_id === dimensionId,
  );

  const generate = useMutation({
    mutationFn: () =>
      generateDictionaryPreview(projectId, revision.id, versionOf(revision), [dimensionId]),
    onSuccess: (result) => {
      setPreview(result);
      setDrafts(
        new Map(
          result.candidates.map((candidate) => [
            candidate.id,
            {
              displayName: candidate.display_name,
              aliases: candidate.aliases.join('，'),
              accept: true,
            },
          ]),
        ),
      );
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const apply = useMutation({
    mutationFn: () =>
      applyDictionaryPreview(
        projectId,
        revision.id,
        versionOf(revision),
        preview!.id,
        preview!.candidates.map((candidate) => {
          const draft = drafts.get(candidate.id)!;
          if (!draft.accept) return { candidate_id: candidate.id, accept: false };
          return {
            candidate_id: candidate.id,
            accept: true,
            display_name: draft.displayName.trim() || candidate.value,
            aliases: draft.aliases.split(/[，,、]/).map((s) => s.trim()).filter(Boolean),
          };
        }),
      ),
    onSuccess: (next) => {
      acceptRevision(next);
      setPreview(null);
      toast.success('维度值字典已应用。');
    },
    onError: (error) => toast.error(describeError(error)),
  });

  const patch = (id: string, change: Partial<Draft>) =>
    setDrafts((prev) => {
      const next = new Map(prev);
      next.set(id, { ...next.get(id)!, ...change });
      return next;
    });

  const blocked = preview?.eligibilities.find((item) => item.status !== 'eligible');

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <span className="text-[13px] font-medium text-slate-700">维度值字典</span>
          <span className="ml-2 text-[11px] text-slate-400">
            {existing?.length
              ? `已有 ${existing.length} 个值`
              : '把真实取值和它们的别名(如「北上广」)固化进目录'}
          </span>
        </div>
        {!readOnly && (
          <Button variant="default" size="sm" loading={generate.isPending} onClick={() => generate.mutate()}>
            {existing?.length ? '重新采集' : '采集取值'}
          </Button>
        )}
      </div>

      {generate.isPending && (
        <div className="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
          <Spinner /> 正在读取真实取值…
        </div>
      )}

      {blocked && (
        <div className="mt-2 rounded border border-amber-200/70 bg-amber-50/60 px-2.5 py-1.5 text-[11px] text-amber-800">
          {blocked.message}
        </div>
      )}

      {preview && !blocked && (
        <div className="mt-2 flex flex-col gap-1.5">
          {preview.candidates.map((candidate) => (
            <CandidateRow
              key={candidate.id}
              candidate={candidate}
              draft={drafts.get(candidate.id)!}
              onChange={(change) => patch(candidate.id, change)}
            />
          ))}
          {preview.candidates.length === 0 && (
            <div className="text-[11px] text-slate-400">没有采集到取值。</div>
          )}
          {preview.candidates.length > 0 && (
            <div className="mt-1 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setPreview(null)}>
                取消
              </Button>
              <Button variant="primary" size="sm" loading={apply.isPending} onClick={() => apply.mutate()}>
                应用 {[...drafts.values()].filter((d) => d.accept).length} 个值
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CandidateRow({
  candidate,
  draft,
  onChange,
}: {
  candidate: AnalyticsDictionaryCandidate;
  draft: Draft;
  onChange: (change: Partial<Draft>) => void;
}) {
  return (
    <div
      className={`flex flex-wrap items-center gap-2 rounded-md border bg-white px-2.5 py-1.5 text-[12px] ${
        draft.accept ? 'border-slate-200' : 'border-slate-100 opacity-50'
      }`}
    >
      <input
        type="checkbox"
        checked={draft.accept}
        onChange={(e) => onChange({ accept: e.target.checked })}
      />
      <span className="font-mono text-slate-700">{candidate.value}</span>
      <span className="text-[10px] text-slate-400">×{candidate.frequency}</span>
      {candidate.current && <span className="text-[10px] text-blue-500">已在目录</span>}
      <Input
        className="h-7 w-28 text-[12px]"
        placeholder="显示名"
        value={draft.displayName}
        disabled={!draft.accept}
        onChange={(e) => onChange({ displayName: e.target.value })}
      />
      <Input
        className="h-7 flex-1 text-[12px]"
        placeholder="别名,用「，」分隔(如 北上广 的成员)"
        value={draft.aliases}
        disabled={!draft.accept}
        onChange={(e) => onChange({ aliases: e.target.value })}
      />
    </div>
  );
}
