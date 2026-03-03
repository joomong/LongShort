'use client';

import React, { startTransition } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { getMetricCellColor } from '@/lib/metricColors';
import type { BacktestAlphaResult } from '@/types';

const EMPTY_ALPHAS: BacktestAlphaResult[] = [];

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

const formatPnl = (value: number | undefined | null) => {
  if (!isFiniteNumber(value)) return '-';
  const v = value;
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}%`;
};

const formatNumber = (value: number | undefined | null) =>
  isFiniteNumber(value) ? value.toFixed(2) : '-';

const metricClass = (metric: string, value: number | undefined | null) =>
  isFiniteNumber(value) ? getMetricCellColor(metric, value) : 'text-foreground-muted';

// Hoisted static thead — column labels never change (rendering-hoist-jsx)
// Order: avgBet | avgMaxBet | maxWeight | minBet | quality | return | sharpe | sharpe400 | turnover
const TABLE_HEADER = (
  <thead className="sticky top-0 bg-background-secondary/50">
    {/* Group Header Row */}
    <tr className="text-[11px] uppercase tracking-wider font-semibold text-foreground-secondary border-b-2 border-border-strong">
      <th className="w-6"></th>
      <th className="text-left w-10">ID</th>
      <th className="text-left w-16">Name</th>
      <th className="text-center pb-0" colSpan={9}>IS</th>
      <th className="text-center pb-0 border-l border-border-strong" colSpan={9}>OS</th>
    </tr>
    {/* Metric Header Row */}
    <tr className="text-xs font-medium text-foreground-secondary border-b border-border-strong">
      <th className="w-6"></th>
      <th className="text-left w-10">ID</th>
      <th className="text-left w-16">Name</th>
      <th className="text-right px-2.5">avgBet</th>
      <th className="text-right px-2.5">avgMaxBet</th>
      <th className="text-right px-2.5">maxWeight</th>
      <th className="text-right px-2.5">minBet</th>
      <th className="text-right px-2.5">quality</th>
      <th className="text-right px-2.5">return</th>
      <th className="text-right px-2.5">sharpe</th>
      <th className="text-right px-2.5">sharpe400</th>
      <th className="text-right px-2.5">turnover</th>
      <th className="text-right px-2.5 border-l border-border-strong">avgBet</th>
      <th className="text-right px-2.5">avgMaxBet</th>
      <th className="text-right px-2.5">maxWeight</th>
      <th className="text-right px-2.5">minBet</th>
      <th className="text-right px-2.5">quality</th>
      <th className="text-right px-2.5">return</th>
      <th className="text-right px-2.5">sharpe</th>
      <th className="text-right px-2.5">sharpe400</th>
      <th className="text-right px-2.5">turnover</th>
    </tr>
  </thead>
);

// React.memo — prevents re-render when parent state (selectedCount, sortedBy) changes
// but this row's item/index props are unchanged (rerender-memo)
const RankRow = React.memo(function RankRow({ item, index }: { item: BacktestAlphaResult; index: number }) {
  const { isMetrics, osMetrics } = item;
  const isCombined = item.rank === 0;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === index);
  const isSelected = useAlphaStore(s => s.selectedAlphaIndexSet.has(index)); // O(1) Set lookup (js-set-map-lookups)
  // Actions read via getState() — not subscribed as selectors (rerender-defer-reads)

  if (!isMetrics || !osMetrics) return null;

  const cellClass = "text-xs font-mono tabular-nums text-right px-3 py-1.5 text-data-color";

  return (
    <tr
      className={
        'rank-row border-b border-border/40 last:border-b-0 cursor-pointer transition-colors ' +
        (isCombined ? 'border-b-2 border-border-strong font-semibold ' : '') +
        (isActiveDetail
          ? 'bg-row-active border-l-2 border-l-accent'
          : 'hover:bg-row-hover')
      }
      onClick={() => startTransition(() => useAlphaStore.getState().enterDetailView(index))}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startTransition(() => useAlphaStore.getState().enterDetailView(index)); } }}
      role="button"
    >
      {/* Selection checkbox */}
      <td className="px-1 py-1 text-center">
        {!isCombined ? (
          <button
            onClick={(e) => { e.stopPropagation(); useAlphaStore.getState().toggleAlphaSelection(index); }}
            className="w-4 h-4 border border-foreground-muted/40 hover:border-accent transition-colors inline-flex items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
            title={isSelected ? 'Deselect' : 'Select'}
            aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
          >
            {isSelected ? <span className="w-2.5 h-2.5 bg-accent" /> : null}
          </button>
        ) : null}
      </td>
      {/* ID */}
      <td className="text-xs text-foreground-muted font-mono tabular-nums px-3 py-1.5">
        {isCombined ? 'batch 0' : String(item.rank - 1)}
      </td>
      {/* Name */}
      <td className="text-xs font-mono px-3 py-1.5 truncate max-w-[100px]" title={item.name}>{item.name}</td>

      {/* IS Metrics — order: avgBet | avgMaxBet | maxWeight | minBet | quality | return | sharpe | sharpe400 | turnover */}
      <td className={cellClass}>{isFiniteNumber(isMetrics.min_bet) ? isMetrics.min_bet.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.avgMaxBet) ? (isMetrics.avgMaxBet * 100).toFixed(1) + '%' : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.max_weight) ? `${(isMetrics.max_weight * 100).toFixed(1)}%` : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.minBetMin) ? isMetrics.minBetMin.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.quality) ? isMetrics.quality.toFixed(2) : '-'}</td>
      <td className={`${cellClass} ${metricClass('pnl', isMetrics.ann_ret ?? isMetrics.pnl)}`}>{formatPnl(isMetrics.ann_ret ?? isMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', isMetrics.sharpe400)}`}>{formatNumber(isMetrics.sharpe400)}</td>
      <td className={`${cellClass} ${metricClass('turnover', isMetrics.turnover)}`}>{isFiniteNumber(isMetrics.turnover) ? isMetrics.turnover.toFixed(1) : '-'}</td>

      {/* OS Metrics — order: avgBet | avgMaxBet | maxWeight | minBet | quality | return | sharpe | sharpe400 | turnover */}
      <td className={`${cellClass} border-l border-border-strong`}>{isFiniteNumber(osMetrics.min_bet) ? osMetrics.min_bet.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(osMetrics.avgMaxBet) ? (osMetrics.avgMaxBet * 100).toFixed(1) + '%' : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(osMetrics.max_weight) ? `${(osMetrics.max_weight * 100).toFixed(1)}%` : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(osMetrics.minBetMin) ? osMetrics.minBetMin.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(osMetrics.quality) ? osMetrics.quality.toFixed(2) : '-'}</td>
      <td className={`${cellClass} ${metricClass('pnl', osMetrics.ann_ret ?? osMetrics.pnl)}`}>{formatPnl(osMetrics.ann_ret ?? osMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', osMetrics.sharpe400)}`}>{formatNumber(osMetrics.sharpe400)}</td>
      <td className={`${cellClass} ${metricClass('turnover', osMetrics.turnover)}`}>{isFiniteNumber(osMetrics.turnover) ? osMetrics.turnover.toFixed(1) : '-'}</td>
    </tr>
  );
});

export default function AlphaRankingTable() {
  const alphas = useAlphaStore(s => s.result?.alphas) ?? EMPTY_ALPHAS;
  const sortedBy = useAlphaStore(s => s.result?.sortedBy);
  const selectedCount = useAlphaStore(s => s.selectedAlphaIndices.length);
  // clearAlphaSelection read via getState() — not subscribed as selector (rerender-defer-reads)

  const hasCombined = alphas.length > 0 && alphas[0].rank === 0;
  const alphaCount = hasCombined ? alphas.length - 1 : alphas.length;

  return (
    <div className="bg-background-tertiary border-b border-border flex-1 flex flex-col min-h-0 panel-inset">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-1.5 border-b border-border bg-background-secondary/50">
        <span className="text-xs text-foreground-muted">
          Alpha Ranking ({alphaCount})
          {selectedCount > 0 ? (
            <>
              {' '}&middot;{' '}
              <span className="text-blue-400">{selectedCount} selected</span>
              {' '}
              <button onClick={() => useAlphaStore.getState().clearAlphaSelection()} className="text-foreground-muted hover:text-foreground underline focus-visible:ring-2 focus-visible:ring-accent">clear</button>
            </>
          ) : null}
        </span>
        <span className="text-xs text-foreground-muted">
          {sortedBy ? `sortedBy=${sortedBy}` : ''}
        </span>
      </div>

      {/* Scrollable Table */}
      <div className="flex-1 overflow-y-auto">
        <table className="w-full border-collapse">
          {TABLE_HEADER}
          <tbody>
            {alphas.map((item, index) => (
              <RankRow key={`${item.name}-${index}`} item={item} index={index} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
