'use client';

import { useMemo, useState } from 'react';
import { useRunStore } from '@/runStore';
import PerformanceChart, { type DetailMetricKey } from './PerformanceChart';
import YearlyMetricsTable from './YearlyMetricsTable';

const METRICS: Array<{ key: DetailMetricKey; label: string }> = [
  { key: 'pnl', label: 'PnL' },
  { key: 'turnover', label: 'Turnover%' },
  { key: 'nonZero', label: 'Non-zero Pos' },
  { key: 'maxWeight', label: 'Max Weight%' },
  { key: 'mdd', label: 'Drawdown' },
];

export default function PerformanceReport() {
  const result = useRunStore(s => s.result);
  const selectedAlphaIndex = useRunStore(s => s.selectedAlphaIndex);
  const request = useRunStore(s => s.selectedAlphaIndex === -1 ? s.combinedRequest : s.detailRequest);
  const [metric, setMetric] = useState<DetailMetricKey>('pnl');

  const alpha = useMemo(() => {
    if (selectedAlphaIndex === null) return null;
    if (selectedAlphaIndex === -1) return result?.combined ?? null;
    return result?.alphas?.[selectedAlphaIndex] ?? null;
  }, [result, selectedAlphaIndex]);

  if (!result || selectedAlphaIndex === null) return null;
  if (!alpha) return request.status === 'error'
    ? <div role="alert" className="px-2 py-1 text-xs text-metric-negative">{request.error}</div>
    : <div role="status" className="px-2 py-1 text-[11px] text-foreground-muted">Loading performance…</div>;

  return (
    <div className="flex flex-col bg-chart-background border-b border-border h-full min-h-0">
      {alpha.error || request.status === 'error' ? <div role="alert" className="px-2 py-1 text-xs text-metric-negative">{alpha.error || (request.status === 'error' ? request.error : '')}</div> : null}
      {request.status === 'loading' && !alpha.error ? <div role="status" className="px-2 py-1 text-[11px] text-foreground-muted">Loading performance…</div> : null}
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
          <PerformanceChart
            alpha={alpha}
            metric={metric}
            context={result.context}
          />
        </div>

        {/* Yearly table */}
        <div className="w-full shrink-0 min-w-0 bg-chart-background pt-1" data-testid="yearly-table-panel">
          <YearlyMetricsTable alpha={alpha} context={result.context} />
        </div>
      </div>
    </div>
  );
}
