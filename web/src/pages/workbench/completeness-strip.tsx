import { useMemo, useState } from 'react';
import type { AnalyticsRevision } from '@analytics/api/types';
import { computeCompleteness, type CompletenessGauge } from './completeness';
import type { StepKey } from './index';

/**
 * 完成度条:常驻工作台头部。上下文缺口不在眼前,用户就要等问数错答时才发现。
 * 全部本地计算;全绿时整条收起,不占注意力。
 */

const TARGET_STEP: Record<CompletenessGauge['key'], StepKey> = {
  descriptions: 'catalog',
  aliases: 'catalog',
  timeAxis: 'catalog',
};

export function CompletenessStrip({
  revision,
  goTo,
}: {
  revision: AnalyticsRevision;
  goTo: (step: StepKey) => void;
}) {
  const gauges = useMemo(() => computeCompleteness(revision), [revision]);
  const [open, setOpen] = useState<CompletenessGauge['key'] | null>(null);
  const applicable = gauges.filter((gauge) => gauge.total > 0);
  const incomplete = applicable.filter((gauge) => gauge.covered < gauge.total);
  if (incomplete.length === 0) return null;

  return (
    <div className="rounded-lg border border-amber-200/70 bg-amber-50/40 px-4 py-2.5 text-xs">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
        <span className="font-medium text-amber-800">建模完成度</span>
        {applicable.map((gauge) => {
          const done = gauge.covered >= gauge.total;
          return (
            <button
              key={gauge.key}
              type="button"
              onClick={() => setOpen(open === gauge.key ? null : gauge.key)}
              className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 transition-colors ${
                done ? 'text-emerald-700' : 'text-amber-800 hover:bg-amber-100/70'
              }`}
            >
              <span>{gauge.label}</span>
              <span className="font-mono">
                {gauge.covered}/{gauge.total}
              </span>
            </button>
          );
        })}
      </div>
      {open && (
        <Detail
          gauge={gauges.find((gauge) => gauge.key === open)!}
          onGo={() => goTo(TARGET_STEP[open])}
        />
      )}
    </div>
  );
}

function Detail({ gauge, onGo }: { gauge: CompletenessGauge; onGo: () => void }) {
  const shown = gauge.missing.slice(0, 12);
  return (
    <div className="mt-2 border-t border-amber-200/60 pt-2 text-amber-900/80">
      <div>{gauge.consequence}。缺失：</div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {shown.map((name, index) => (
          <span key={index} className="rounded bg-white/70 px-1.5 py-0.5 text-amber-900">
            {name}
          </span>
        ))}
        {gauge.missing.length > shown.length && (
          <span className="text-amber-700">…共 {gauge.missing.length} 项</span>
        )}
        <button type="button" onClick={onGo} className="ml-1 font-medium underline">
          去补全
        </button>
      </div>
    </div>
  );
}
