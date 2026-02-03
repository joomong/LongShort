'use client';

import { useAlphaStore } from '@/store/alphaStore';
import { getMetricColor } from '@/lib/metricColors';
import type { BacktestAlphaResult } from '@/types';

const formatPnl = (value: number | undefined) => {
  const v = value ?? 0;
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}%`;
};

const formatNumber = (value: number | undefined) => {
  const v = value ?? 0;
  return v.toFixed(2);
};

function ResultRow({ item, index }: { item: BacktestAlphaResult; index: number }) {
  const { isMetrics, osMetrics } = item;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === index);
  const isSelected = useAlphaStore(s => s.selectedAlphaIndices.includes(index));
  const enterDetailView = useAlphaStore(s => s.enterDetailView);
  const toggleAlphaSelection = useAlphaStore(s => s.toggleAlphaSelection);

  if (!isMetrics || !osMetrics) return null;

  const cellClass = "text-[11px] font-mono tabular-nums text-right px-2.5 py-1";

  return (
    <tr
      className={
        'border-b border-border/40 cursor-pointer ' +
        (isActiveDetail
          ? 'bg-blue-950/30'
          : 'hover:bg-row-hover')
      }
      onClick={() => enterDetailView(index)}
    >
      {/* Selection checkbox */}
      <td className="px-1 py-1 text-center">
        <button
          onClick={(e) => { e.stopPropagation(); toggleAlphaSelection(index); }}
          className="w-3.5 h-3.5 rounded-full border border-foreground-muted/40 hover:border-accent transition-colors inline-flex items-center justify-center"
          title={isSelected ? 'Deselect' : 'Select'}
        >
          {isSelected && <span className="w-2 h-2 rounded-full bg-blue-500" />}
        </button>
      </td>
      {/* Index */}
      <td className="text-[11px] text-foreground-muted font-mono tabular-nums px-2.5 py-1">#{index + 1}</td>

      {/* IS Metrics */}
      <td className={`${cellClass} ${getMetricColor('pnl', isMetrics.pnl)}`}>{formatPnl(isMetrics.pnl)}</td>
      <td className={`${cellClass} ${getMetricColor('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</td>
      <td className={`${cellClass} ${getMetricColor('mdd', isMetrics.mdd ?? 0)}`}>{formatPnl(isMetrics.mdd)}</td>
      <td className={`${cellClass} ${getMetricColor('turnover', isMetrics.turnover ?? 0)}`}>{(isMetrics.turnover ?? 0).toFixed(0)}</td>
      <td className={cellClass}>{(isMetrics.min_bet ?? 0).toFixed(0)}</td>
      <td className={cellClass}>{((isMetrics.max_weight ?? 0) * 100).toFixed(1)}%</td>
      <td className={`${cellClass} ${getMetricColor('ir', isMetrics.ir ?? 0)}`}>{formatNumber(isMetrics.ir)}</td>
      <td className={`${cellClass} ${getMetricColor('corr', isMetrics.corr ?? 0)}`}>{formatNumber(isMetrics.corr)}</td>

      {/* OS Metrics — thicker left border for group separator */}
      <td className={`${cellClass} border-l border-border-strong ${getMetricColor('pnl', osMetrics.pnl)}`}>{formatPnl(osMetrics.pnl)}</td>
      <td className={`${cellClass} ${getMetricColor('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</td>
      <td className={`${cellClass} ${getMetricColor('mdd', osMetrics.mdd ?? 0)}`}>{formatPnl(osMetrics.mdd)}</td>
      <td className={`${cellClass} ${getMetricColor('ir', osMetrics.ir ?? 0)}`}>{formatNumber(osMetrics.ir)}</td>
    </tr>
  );
}

export default function BacktestMetrics() {
  const alphas = useAlphaStore(s => s.result?.alphas ?? []);

  return (
    <div className="bg-background-tertiary border-b border-border">
      {/* Scrollable Results Table */}
      <div className="h-32 overflow-y-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-background-tertiary">
            {/* Group Header Row */}
            <tr className="text-[10px] font-semibold text-foreground-secondary border-b-2 border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10">#</th>
              <th className="text-center py-0.5" colSpan={8}>IS</th>
              <th className="text-center py-0.5 border-l border-border-strong" colSpan={4}>OS</th>
            </tr>
            {/* Metric Header Row */}
            <tr className="text-[11px] font-medium text-foreground-secondary border-b border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10 px-2.5">#</th>
              <th className="text-right px-2.5">PnL</th>
              <th className="text-right px-2.5">Sharpe</th>
              <th className="text-right px-2.5">MDD</th>
              <th className="text-right px-2.5">Turn</th>
              <th className="text-right px-2.5">MinBet</th>
              <th className="text-right px-2.5">MaxWt</th>
              <th className="text-right px-2.5">IR</th>
              <th className="text-right px-2.5">Corr</th>
              <th className="text-right px-2.5 border-l border-border-strong">PnL</th>
              <th className="text-right px-2.5">Sharpe</th>
              <th className="text-right px-2.5">MDD</th>
              <th className="text-right px-2.5">IR</th>
            </tr>
          </thead>
          <tbody>
            {alphas.map((item, index) => (
              <ResultRow key={`${item.name}-${index}`} item={item} index={index} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
