'use client';

import React, { startTransition } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { getMetricCellColor } from '@/lib/metricColors';
import type { BacktestAlphaResult, PeriodMetrics } from '@/types';

const EMPTY_ALPHAS: BacktestAlphaResult[] = [];

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

const formatPnl = (value: number | undefined | null) => {
  if (!isFiniteNumber(value)) return '-';
  const v = value;
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(2)}%`;
};

const formatNumber = (value: number | undefined | null) =>
  isFiniteNumber(value) ? value.toFixed(2) : '-';

function MetricValue({ metric, value, children }: { metric: string; value: number | undefined | null; children: React.ReactNode }) {
  const color = isFiniteNumber(value) ? getMetricCellColor(metric, value) : '';
  const underline = color === 'text-metric-positive'
    ? 'underline decoration-metric-positive decoration-2 underline-offset-[3px]'
    : color === 'text-metric-negative'
      ? 'underline decoration-metric-negative decoration-2 underline-offset-[3px]'
      : '';
  return <span className={underline}>{children}</span>;
}

// Hoisted static thead — column labels never change (rendering-hoist-jsx)
// Order: avgBet | avgMaxWeight | maxWeight | minBet | quality | return | sharpe | sharpe400 | sharpe80 | turnover
const TABLE_HEADER = (
  <thead className="sticky top-0 bg-white dark:bg-background-secondary">
    {/* Group Header Row */}
    <tr className="text-[10px] font-semibold text-[#111111] dark:text-foreground">
      <th className="text-left w-12">ID</th>
      <th className="w-6"></th>
      <th className="text-center pb-0" colSpan={10}><span className="block mx-2 border-b border-foreground-muted">IS</span></th>
      <th className="text-center pb-0" colSpan={10}><span className="block mx-2 border-b border-foreground-muted">OS</span></th>
    </tr>
    {/* Metric Header Row */}
    <tr className="text-[10px] font-medium text-[#111111] dark:text-foreground border-b border-border-strong">
      <th className="w-12"></th>
      <th className="w-6"></th>
      <th className="text-right px-1.5">avgBet</th>
      <th className="text-right px-1.5">avgMaxWeight</th>
      <th className="text-right px-1.5">maxWeight</th>
      <th className="text-right px-1.5">minBet</th>
      <th className="text-right px-1.5">quality</th>
      <th className="text-right px-1.5">return</th>
      <th className="text-right px-1.5">sharpe</th>
      <th className="text-right px-1.5">sharpe400</th>
      <th className="text-right px-1.5">sharpe80</th>
      <th className="text-right px-1.5">turnover</th>
      <th className="text-right px-1.5">avgBet</th>
      <th className="text-right px-1.5">avgMaxWeight</th>
      <th className="text-right px-1.5">maxWeight</th>
      <th className="text-right px-1.5">minBet</th>
      <th className="text-right px-1.5">quality</th>
      <th className="text-right px-1.5">return</th>
      <th className="text-right px-1.5">sharpe</th>
      <th className="text-right px-1.5">sharpe400</th>
      <th className="text-right px-1.5">sharpe80</th>
      <th className="text-right px-1.5">turnover</th>
    </tr>
  </thead>
);

function MetricCells({ m }: { m: PeriodMetrics }) {
  const cellClass = "text-[10px] font-sans font-semibold tabular-nums text-right px-1.5 py-0.5 text-[#111111] dark:text-foreground";
  return (
    <>
      <td className={cellClass}>{isFiniteNumber(m.avgPositionCount) ? m.avgPositionCount.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.avgMaxBet) ? m.avgMaxBet.toFixed(2) + '%' : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.max_weight) ? `${m.max_weight.toFixed(2)}%` : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.minPositionCount) ? m.minPositionCount.toFixed(0) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.quality) ? m.quality.toFixed(2) : '-'}</td>
      <td className={cellClass}><MetricValue metric="pnl" value={m.ann_ret ?? m.pnl}>{formatPnl(m.ann_ret ?? m.pnl)}</MetricValue></td>
      <td className={cellClass}><MetricValue metric="sharpe" value={m.sharpe}>{formatNumber(m.sharpe)}</MetricValue></td>
      <td className={cellClass}>{formatNumber(m.sharpe400)}</td>
      <td className={cellClass}><MetricValue metric="sharpe" value={m.sharpe80}>{formatNumber(m.sharpe80)}</MetricValue></td>
      <td className={cellClass}>{isFiniteNumber(m.turnover) ? m.turnover.toFixed(2) + '%' : '-'}</td>
    </>
  );
}

// React.memo — BatchRow has no props; memo prevents parent re-renders from calling this
// component. Zustand selectors inside still trigger re-renders when their values change.
const BatchRow = React.memo(function BatchRow() {
  const combined = useAlphaStore(s => s.result?.combined);
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === -1);
  const alphaCount = useAlphaStore(s => s.result?.alphas.length ?? 0);
  const selectedCount = useAlphaStore(s => s.result?.alphas.reduce(
    (count, _, index) => count + Number(s.selectedAlphaIndexSet.has(index)), 0
  ) ?? 0);
  const allSelected = alphaCount > 0 && selectedCount === alphaCount;
  const partiallySelected = selectedCount > 0 && !allSelected;
  if (!combined) return null;

  const { isMetrics, osMetrics } = combined;
  if (!isMetrics || !osMetrics) return null;

  return (
    <tr
      className={
        'rank-row border-b border-border-strong cursor-pointer transition-colors ' +
        (isActiveDetail
          ? 'bg-[#e0ddf5] dark:bg-[#38354d]'
          : 'bg-white dark:bg-background-tertiary hover:bg-[#f1effa] dark:hover:bg-row-hover')
      }
      onClick={() => startTransition(() => useAlphaStore.getState().enterDetailView(-1))}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startTransition(() => useAlphaStore.getState().enterDetailView(-1)); } }}
      role="button"
      aria-expanded={isActiveDetail}
      aria-label="View combined alpha"
    >
      <td className="text-[10px] text-[#111111] dark:text-foreground font-sans font-semibold tabular-nums px-1.5 py-0.5">batch</td>
      <td className="px-1 py-0.5 text-center leading-[14px]">
        <button
          onClick={(e) => { e.stopPropagation(); useAlphaStore.getState().toggleAllAlphaSelection(); }}
          onKeyDown={(e) => e.stopPropagation()}
          aria-pressed={partiallySelected ? 'mixed' : allSelected}
          aria-label={allSelected ? 'Deselect all alphas' : 'Select all alphas'}
          title={allSelected ? 'Deselect all alphas' : 'Select all alphas'}
          disabled={alphaCount === 0}
          className="w-3 h-3 rounded-full border border-[#777777] dark:border-foreground-muted hover:border-accent transition-colors inline-flex align-middle items-center justify-center focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
        >
          {allSelected ? <span className="w-1.5 h-1.5 rounded-full bg-foreground-secondary" /> : partiallySelected ? <span className="w-1.5 h-px bg-foreground-secondary" /> : null}
        </button>
      </td>
      <MetricCells m={isMetrics} />
      <MetricCells m={osMetrics} />
    </tr>
  );
});

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
          ? 'bg-[#e0ddf5] dark:bg-[#38354d]'
          : 'bg-white dark:bg-background-tertiary hover:bg-[#f1effa] dark:hover:bg-row-hover')
      }
      onClick={() => startTransition(() => useAlphaStore.getState().enterDetailView(index))}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startTransition(() => useAlphaStore.getState().enterDetailView(index)); } }}
      role="button"
      aria-expanded={isActiveDetail}
      aria-label={`View ${item.name}`}
    >
      {/* ID */}
      <td className="text-[10px] text-[#111111] dark:text-foreground font-sans font-semibold tabular-nums px-1.5 py-0.5">
        {String(item.rank - 1)}
      </td>
      {/* Save selection */}
      <td className="px-1 py-0.5 text-center leading-[14px]">
        <button
          onClick={(e) => { e.stopPropagation(); useAlphaStore.getState().toggleAlphaSelection(index); }}
          onKeyDown={(e) => e.stopPropagation()}
          aria-pressed={isSelected}
          className="w-3 h-3 rounded-full border border-[#777777] dark:border-foreground-muted hover:border-accent transition-colors inline-flex align-middle items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
          title={isSelected ? 'Deselect' : 'Select'}
          aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
        >
          {isSelected ? <span className="w-1.5 h-1.5 rounded-full bg-foreground-secondary" /> : null}
        </button>
      </td>
      <MetricCells m={isMetrics} />
      <MetricCells m={osMetrics} />
    </tr>
  );
});

export default function AlphaRankingTable() {
  const alphas = useAlphaStore(s => s.result?.alphas) ?? EMPTY_ALPHAS;
  const selectedCount = useAlphaStore(s => s.selectedAlphaIndices.length);
  const hasCombined = useAlphaStore(s => s.result?.combined != null);
  // clearAlphaSelection read via getState() — not subscribed as selector (rerender-defer-reads)


  return (
    <div className="bg-background-tertiary border-b border-border flex flex-col min-h-0">
      {selectedCount > 0 ? (
        <div className="flex items-center gap-2 px-2 py-0.5 border-b border-border bg-background-secondary text-[10px]">
          <span className="text-accent">{selectedCount} selected</span>
          <button onClick={() => useAlphaStore.getState().clearAlphaSelection()} className="text-foreground-muted hover:text-foreground underline focus-visible:ring-2 focus-visible:ring-accent">clear</button>
        </div>
      ) : null}

      {/* Reserve the two header rows plus five compact result rows, even for a single alpha. */}
      <div className="min-h-[132px] overflow-auto pr-6 bg-white dark:bg-background-tertiary">
        <table aria-label="Alpha results" className="w-full border-collapse whitespace-nowrap text-[10px] leading-[14px] text-[#111111] dark:text-foreground">
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
