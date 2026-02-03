'use client';

import { useMemo, useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import DetailTimeSeries, { type DetailMetricKey } from './DetailTimeSeries';
import DetailSummaryTable from './DetailSummaryTable';

const METRICS: Array<{ key: DetailMetricKey; label: string }> = [
  { key: 'pnl', label: 'PnL' },
  { key: 'mdd', label: 'MDD' },
  { key: 'minBet', label: 'MinBet' },
  { key: 'maxWeight', label: 'MaxWeight' },
  { key: 'nonZero', label: 'Non-zero Position' },
];

export default function AlphaDetailView() {
  const result = useAlphaStore(s => s.result);
  const selectedAlphaIndex = useAlphaStore(s => s.selectedAlphaIndex);
  const [metric, setMetric] = useState<DetailMetricKey>('pnl');

  const alpha = useMemo(() => {
    if (selectedAlphaIndex === null) return null;
    return result?.alphas?.[selectedAlphaIndex] ?? null;
  }, [result, selectedAlphaIndex]);

  if (!alpha) return null;

  return (
    <div className="flex flex-col bg-background-tertiary border-b border-border">
      {/* ① metric selector row (like the wireframe small boxes) */}
      <div className="flex items-center gap-1 px-2 py-1 border-b border-border">
        {METRICS.map((m) => {
          const active = m.key === metric;
          return (
            <button
              key={m.key}
              onClick={() => setMetric(m.key)}
              className={
                'text-[11px] px-2 py-1 rounded border transition-colors ' +
                (active
                  ? 'bg-background-secondary border-border text-foreground'
                  : 'bg-background-tertiary border-border/40 text-foreground-muted hover:text-foreground hover:bg-background-secondary/40')
              }
              title={m.label}
            >
              {m.label}
            </button>
          );
        })}
      </div>

      {/* ② chart block */}
      <div className="px-2 py-2">
        <div className="h-[260px] border border-border rounded bg-background overflow-hidden">
          <DetailTimeSeries alpha={alpha} metric={metric} />
        </div>
      </div>

      {/* ③ yearly table block (calendar year) */}
      <div className="px-2 pb-2">
        <div className="border border-border rounded bg-background overflow-hidden">
          <DetailSummaryTable alpha={alpha} initialCapital={result?.initialCapital} />
        </div>
      </div>
    </div>
  );
}
