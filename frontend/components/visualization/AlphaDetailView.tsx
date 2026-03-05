'use client';

import { useMemo, useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import DetailTimeSeries, { type DetailMetricKey } from './DetailTimeSeries';
import DetailSummaryTable from './DetailSummaryTable';

const METRICS: Array<{ key: DetailMetricKey; label: string }> = [
  { key: 'pnl', label: 'PnL' },
  { key: 'turnover', label: 'Turnover%' },
  { key: 'nonZero', label: 'Non-zero Pos' },
  { key: 'maxWeight', label: 'Max Weight%' },
  { key: 'mdd', label: 'Drawdown' },
];

// Grid background for chart container
const CHART_GRID_STYLE: React.CSSProperties = {
  backgroundImage:
    'linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px),' +
    'linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px)',
  backgroundSize: '48px 48px',
};

export default function AlphaDetailView() {
  const result = useAlphaStore(s => s.result);
  const selectedAlphaIndex = useAlphaStore(s => s.selectedAlphaIndex);
  const [metric, setMetric] = useState<DetailMetricKey>('pnl');

  const exitDetailView = useAlphaStore(s => s.exitDetailView);

  const alpha = useMemo(() => {
    if (selectedAlphaIndex === null) return null;
    if (selectedAlphaIndex === -1) return result?.combined ?? null;
    return result?.alphas?.[selectedAlphaIndex] ?? null;
  }, [result, selectedAlphaIndex]);

  if (!alpha) return null;

  return (
    <div className="flex flex-col bg-background-tertiary border-b border-border h-full">
      {/* header: alpha name + close button */}
      <div className="flex items-center justify-between px-2 py-1 border-b border-border shrink-0">
        <span className="text-[11px] text-foreground-muted">
          {alpha.name === 'combined' ? 'Combined (batch 0)' : `#${alpha.rank} ${alpha.name}`}
        </span>
        <button
          onClick={exitDetailView}
          className="text-[11px] text-foreground-muted hover:text-foreground px-1"
          title="Close detail view"
        >
          ✕
        </button>
      </div>
      {/* metric selector row */}
      <div className="flex items-center gap-1 px-2 py-1 border-b border-border shrink-0">
        {METRICS.map((m) => {
          const active = m.key === metric;
          return (
            <button
              key={m.key}
              onClick={() => setMetric(m.key)}
              className={
                'text-[11px] px-2 py-1 border transition-colors ' +
                (active
                  ? 'bg-accent/10 border-accent/30 text-accent'
                  : 'bg-background-tertiary border-border/40 text-foreground-muted hover:text-foreground hover:bg-background-secondary/40')
              }
              title={m.label}
            >
              {m.label}
            </button>
          );
        })}
      </div>

      {/* chart + yearly table */}
      <div className="flex-1 flex flex-col min-h-0">
        {/* Chart */}
        <div className="flex-[2] min-h-[150px] min-w-0 border-b border-border overflow-hidden" style={CHART_GRID_STYLE}>
          <DetailTimeSeries
            alpha={alpha}
            metric={metric}
            initialCapital={result?.initialCapital}
          />
        </div>

        {/* Yearly table */}
        <div className="flex-1 min-h-0 min-w-0 bg-background-secondary overflow-auto">
          <DetailSummaryTable alpha={alpha} />
        </div>
      </div>
    </div>
  );
}
