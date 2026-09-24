'use client';

import { useMemo, useState, useRef } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import DetailTimeSeries, { type DetailMetricKey } from './DetailTimeSeries';
import DetailSummaryTable from './DetailSummaryTable';
import { useContainerSize } from '@/lib/uplotAdapters';

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
  const bodyRef = useRef<HTMLDivElement>(null);
  const bodySize = useContainerSize(bodyRef);

  const alpha = useMemo(() => {
    if (selectedAlphaIndex === null) return null;
    if (selectedAlphaIndex === -1) return result?.combined ?? null;
    return result?.alphas?.[selectedAlphaIndex] ?? null;
  }, [result, selectedAlphaIndex]);

  // Match the reference plot's 1.7:1 drawing area, accounting for axes/legend.
  // Reserve visible yearly rows; shorter windows scroll rather than clip the plot.
  const tableReserve = Math.min(160, Math.max(100, ((alpha?.yearly?.length ?? 0) + 1) * 20));
  const preferredChartHeight = Math.max(200, (Math.min(bodySize.width, 900) - 80) / 1.7 + 72);
  const chartHeight = Math.min(preferredChartHeight, Math.max(150, bodySize.height - tableReserve));

  if (!alpha) return null;

  return (
    <div className="flex flex-col bg-chart-background border-b border-border h-full min-h-0">
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

      {/* chart + yearly table */}
      <div ref={bodyRef} className="flex-1 flex flex-col min-h-0 overflow-auto">
        {/* Chart */}
        <div className="w-full max-w-[900px] shrink-0 min-w-0 border-b border-border overflow-hidden bg-chart-background" style={{ height: chartHeight }} data-testid="performance-chart-panel">
          <DetailTimeSeries
            alpha={alpha}
            metric={metric}
          />
        </div>

        {/* Yearly table */}
        <div className="flex-1 min-h-[72px] min-w-0 bg-chart-background overflow-auto" data-testid="yearly-table-panel">
          <DetailSummaryTable alpha={alpha} />
        </div>
      </div>
    </div>
  );
}
