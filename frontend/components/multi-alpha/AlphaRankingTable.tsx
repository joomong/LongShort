'use client';

import React, { startTransition } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { getMetricCellColor } from '@/lib/metricColors';
import type { BacktestAlphaResult, ISMetrics, OSMetrics } from '@/types';

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
// Order: avgBet | avgMaxBet | maxWeight | minBet | quality | return | sharpe | sharpe80 | turnover
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
      <th className="text-right px-2.5">sharpe80</th>
      <th className="text-right px-2.5">turnover</th>
      <th className="text-right px-2.5 border-l border-border-strong">avgBet</th>
      <th className="text-right px-2.5">avgMaxBet</th>
      <th className="text-right px-2.5">maxWeight</th>
      <th className="text-right px-2.5">minBet</th>
      <th className="text-right px-2.5">quality</th>
      <th className="text-right px-2.5">return</th>
      <th className="text-right px-2.5">sharpe</th>
      <th className="text-right px-2.5">sharpe80</th>
      <th className="text-right px-2.5">turnover</th>
    </tr>
  </thead>
);

function MetricCells({ m, borderLeft }: { m: ISMetrics | OSMetrics; borderLeft?: boolean }) {
  const cellClass = "text-xs font-mono tabular-nums text-right px-3 py-1.5 text-data-color";
  return (
    <>
      <td className={`${cellClass}${borderLeft ? ' border-l border-border-strong' : ''}`}>{isFiniteNumber(m.min_bet) ? m.min_bet.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.avgMaxBet) ? m.avgMaxBet.toFixed(1) + '%' : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.max_weight) ? `${m.max_weight.toFixed(1)}%` : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.minBetMin) ? m.minBetMin.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.quality) ? m.quality.toFixed(2) : '-'}</td>
      <td className={`${cellClass} ${metricClass('pnl', m.ann_ret ?? m.pnl)}`}>{formatPnl(m.ann_ret ?? m.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', m.sharpe)}`}>{formatNumber(m.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', m.sharpe80)}`}>{formatNumber(m.sharpe80)}</td>
      <td className={`${cellClass} ${metricClass('turnover', m.turnover)}`}>{isFiniteNumber(m.turnover) ? m.turnover.toFixed(1) + '%' : '-'}</td>
    </>
  );
}

// Batch row — combined alpha (rank 0 equivalent)
function BatchRow() {
  const combined = useAlphaStore(s => s.result?.combined);
  if (!combined) return null;

  const { isMetrics, osMetrics } = combined;
  if (!isMetrics || !osMetrics) return null;

  return (
    <tr className="rank-row border-b-2 border-border-strong bg-background-secondary/30">
      <td className="px-1 py-1"></td>
      <td className="text-xs text-foreground-muted font-mono tabular-nums px-3 py-1.5 font-semibold">batch</td>
      <td className="text-xs font-mono px-3 py-1.5 font-semibold text-foreground-secondary">0</td>
      <MetricCells m={isMetrics} />
      <MetricCells m={osMetrics} borderLeft />
    </tr>
  );
}

// React.memo — prevents re-render when parent state (selectedCount, sortedBy) changes
// but this row's item/index props are unchanged (rerender-memo)
const RankRow = React.memo(function RankRow({ item, index }: { item: BacktestAlphaResult; index: number }) {
  const { isMetrics, osMetrics } = item;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === index);
  const isSelected = useAlphaStore(s => s.selectedAlphaIndexSet.has(index)); // O(1) Set lookup (js-set-map-lookups)
  // Actions read via getState() — not subscribed as selectors (rerender-defer-reads)

  if (!isMetrics || !osMetrics) return null;

  return (
    <tr
      className={
        'rank-row border-b border-border/40 last:border-b-0 cursor-pointer transition-colors ' +
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
        <button
          onClick={(e) => { e.stopPropagation(); useAlphaStore.getState().toggleAlphaSelection(index); }}
          className="w-4 h-4 border border-foreground-muted/40 hover:border-accent transition-colors inline-flex items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
          title={isSelected ? 'Deselect' : 'Select'}
          aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
        >
          {isSelected ? <span className="w-2.5 h-2.5 bg-accent" /> : null}
        </button>
      </td>
      {/* ID */}
      <td className="text-xs text-foreground-muted font-mono tabular-nums px-3 py-1.5">
        {String(item.rank - 1)}
      </td>
      {/* Name */}
      <td className="text-xs font-mono px-3 py-1.5 truncate max-w-[100px]" title={item.name}>{item.name}</td>
      <MetricCells m={isMetrics} />
      <MetricCells m={osMetrics} borderLeft />
    </tr>
  );
});

export default function AlphaRankingTable() {
  const alphas = useAlphaStore(s => s.result?.alphas) ?? EMPTY_ALPHAS;
  const sortedBy = useAlphaStore(s => s.result?.sortedBy);
  const selectedCount = useAlphaStore(s => s.selectedAlphaIndices.length);
  const hasCombined = useAlphaStore(s => s.result?.combined != null);
  // clearAlphaSelection read via getState() — not subscribed as selector (rerender-defer-reads)

  const alphaCount = alphas.length;

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
            {hasCombined ? <BatchRow /> : null}
            {alphas.map((item, index) => (
              <RankRow key={`${item.name}-${index}`} item={item} index={index} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
