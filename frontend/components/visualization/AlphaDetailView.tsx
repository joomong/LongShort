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

export default function AlphaDetailView() {
  const result = useAlphaStore(s => s.result);
  const selectedAlphaIndex = useAlphaStore(s => s.selectedAlphaIndex);
  const [metric, setMetric] = useState<DetailMetricKey>('pnl');

  const alpha = useMemo(() => {
    if (selectedAlphaIndex === null) return null;
    if (selectedAlphaIndex === -1) return result?.combined ?? null;
    return result?.alphas?.[selectedAlphaIndex] ?? null;
  }, [result, selectedAlphaIndex]);

  if (!alpha) return null;

  return (
    <div className="flex flex-col bg-chart-background border-b border-border h-full min-h-0">
      {alpha.error || alpha.detailError ? <div role="alert" className="px-2 py-1 text-xs text-metric-negative">{alpha.error || alpha.detailError}</div> : null}
      {alpha.detailLoaded === false && !alpha.error && !alpha.detailError ? <div role="status" className="px-2 py-1 text-[11px] text-foreground-muted">Loading performance…</div> : null}
      {/* metric selector row */}
      <div className="flex items-center px-1 border-b border-border shrink-0 min-w-0 bg-background-secondary">
        <div className="flex flex-1 min-w-0 items-center overflow-x-auto">
        {METRICS.map((m) => {
          const active = m.key === metric;
          return (
            <button
              key={m.key}
              onClick={() => setMetric(m.key)}
              className={
                'shrink-0 text-[10px] px-2 py-1 border-r border-border whitespace-nowrap transition-colors ' +
                (active
                  ? 'bg-accent text-white font-semibold'
                  : 'text-foreground-secondary hover:text-foreground hover:bg-row-hover')
              }
              title={m.label}
              aria-pressed={active}
            >
              {m.label}
            </button>
          );
        })}
        </div>
      </div>

      {/* WQ reference: a bounded landscape chart followed immediately by all years.
          Extra viewport height stays below the report, rather than stretching the plot. */}
      <div className="flex-1 flex flex-col min-h-0 overflow-y-auto" data-testid="performance-report">
        {/* Chart */}
        <div className="w-full aspect-[2/1] flex-[0_1_auto] min-h-[140px] max-h-[480px] min-w-0 overflow-hidden bg-chart-background" data-testid="performance-chart-panel">
          <DetailTimeSeries
            alpha={alpha}
            metric={metric}
          />
        </div>

        {/* Yearly table */}
        <div className="w-full shrink-0 min-w-0 bg-chart-background pt-1" data-testid="yearly-table-panel">
          <DetailSummaryTable alpha={alpha} />
        </div>
      </div>
    </div>
  );
}
