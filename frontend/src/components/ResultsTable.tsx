'use client';

import React, { startTransition } from 'react';
import { useRunStore, visibleCandidateRows } from '@/runStore';
import { getMetricCellColor } from '@/chartStyle';
import { formatNumber, formatPercent } from '@/format';
import type { AlphaResult, SummaryMetrics, PoolCorrelation } from '@/types';

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);


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
// Order: avgBet | avgMaxWeight | maxWeight | minBet | quality | return | sharpe | sharpe400 | turnover
const QUALITY_DESCRIPTION = 'Sharpe × sqrt(abs(1 − max net exposure)) × (1 − max single-symbol weight). Net exposure = abs(long − absolute short); both maxima are taken over this IS/OS period, with weights as fractions of the gross=1 book.';
const POOL_CORR_DESCRIPTION = 'Correlation of daily net returns with the equal-weight alpha pool over the whole sim window (IS and OS).';

const TABLE_HEADER = (
  <thead className="sticky top-0 bg-white dark:bg-background-secondary">
    {/* Group Header Row */}
    <tr className="text-[10px] font-semibold text-[#111111] dark:text-foreground">
      <th className="text-left w-12">ID</th>
      <th className="w-6"></th>
      <th className="text-center pb-0" colSpan={9}><span className="block mx-2 border-b border-foreground-muted">IS</span></th>
      <th className="text-center pb-0" colSpan={9}><span className="block mx-2 border-b border-foreground-muted">OS</span></th>
      <th className="text-center pb-0"><span className="block mx-2 border-b border-foreground-muted">Corr</span></th>
    </tr>
    {/* Metric Header Row */}
    <tr className="text-[10px] font-medium text-[#111111] dark:text-foreground border-b border-border-strong">
      <th className="w-12"></th>
      <th className="w-6"></th>
      <th className="text-right px-1.5">avgBet</th>
      <th className="text-right px-1.5">avgMaxWeight</th>
      <th className="text-right px-1.5">maxWeight</th>
      <th className="text-right px-1.5">minBet</th>
      <th className="text-right px-1.5" title={QUALITY_DESCRIPTION}>quality</th>
      <th className="text-right px-1.5">return</th>
      <th className="text-right px-1.5">sharpe</th>
      <th className="text-right px-1.5">sharpe400</th>
      <th className="text-right px-1.5">turnover</th>
      <th className="text-right px-1.5">avgBet</th>
      <th className="text-right px-1.5">avgMaxWeight</th>
      <th className="text-right px-1.5">maxWeight</th>
      <th className="text-right px-1.5">minBet</th>
      <th className="text-right px-1.5" title={QUALITY_DESCRIPTION}>quality</th>
      <th className="text-right px-1.5">return</th>
      <th className="text-right px-1.5">sharpe</th>
      <th className="text-right px-1.5">sharpe400</th>
      <th className="text-right px-1.5">turnover</th>
      <th className="text-right px-1.5" title={POOL_CORR_DESCRIPTION}>pool</th>
    </tr>
  </thead>
);

const MetricCells = React.memo(function MetricCells({ m }: { m: SummaryMetrics }) {
  const cellClass = "text-[10px] font-sans font-semibold tabular-nums text-right px-1.5 py-0.5 text-[#111111] dark:text-foreground";
  return (
    <>
      <td className={cellClass}>{isFiniteNumber(m.avgPositionCount) ? m.avgPositionCount.toFixed(2) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.avgMaxWeight) ? formatPercent(m.avgMaxWeight) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.maxWeight) ? formatPercent(m.maxWeight) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.minPositionCount) ? m.minPositionCount.toFixed(0) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(m.quality) ? m.quality.toFixed(2) : '-'}</td>
      <td className={cellClass}><MetricValue metric="pnl" value={m.annualReturn}>{formatPercent(m.annualReturn, 2, true)}</MetricValue></td>
      <td className={cellClass}><MetricValue metric="sharpe" value={m.sharpe}>{formatNumber(m.sharpe)}</MetricValue></td>
      <td className={cellClass}>{formatNumber(m.sharpe400)}</td>
      <td className={cellClass}>{isFiniteNumber(m.turnover) ? formatPercent(m.turnover) : '-'}</td>
    </>
  );
});

const PoolCorrCell = React.memo(function PoolCorrCell({ corr }: { corr: PoolCorrelation | undefined }) {
  const cellClass = "text-[10px] font-sans font-semibold tabular-nums text-right px-1.5 py-0.5 text-[#111111] dark:text-foreground";
  if (!corr) return <td className={cellClass} title="No alpha pool configured">-</td>;
  return <td className={cellClass} title={`EW pool correlation over ${corr.days} days`} aria-label="Pool correlation">{formatNumber(corr.ew)}</td>;
});

// React.memo — BatchRow has no props; memo prevents parent re-renders from calling this
// component. Zustand selectors inside still trigger re-renders when their values change.
const BatchRow = React.memo(function BatchRow() {
  const combined = useRunStore(s => s.result?.combined);
  const request = useRunStore(s => s.combinedRequest);
  const isActiveDetail = useRunStore(s => s.selectedAlphaIndex === -1);
  const alphaCount = useRunStore(s => s.result?.alphas.filter(alpha =>
    !s.gaSession || s.activeResultTab === 'stats' || alpha.round === s.activeResultTab).length ?? 0);
  const selectedCount = useRunStore(s => s.result?.alphas.reduce(
    (count, alpha, index) => count + Number((!s.gaSession || s.activeResultTab === 'stats' || alpha.round === s.activeResultTab) && s.selectedAlphaIndexSet.has(index)), 0
  ) ?? 0);
  const allSelected = alphaCount > 0 && selectedCount === alphaCount;
  const partiallySelected = selectedCount > 0 && !allSelected;
  if (!combined) return <tr className="border-b border-border-strong">
    <td className="px-1.5 text-[10px]">batch</td><td colSpan={20} className="px-2 text-[10px] text-foreground-muted">
      {request.status === 'error' ? request.error : 'Loading combined performance…'}
    </td>
  </tr>;

  const isMetrics = combined.summary?.is, osMetrics = combined.summary?.os;
  if (!isMetrics || !osMetrics) return null;

  return (
    <tr
      className={
        'rank-row border-b border-border-strong cursor-pointer transition-colors ' +
        (isActiveDetail
          ? 'bg-[#e0ddf5] dark:bg-[#38354d]'
          : 'bg-white dark:bg-background-tertiary hover:bg-[#f1effa] dark:hover:bg-row-hover')
      }
      onClick={() => startTransition(() => useRunStore.getState().enterDetailView(-1))}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startTransition(() => useRunStore.getState().enterDetailView(-1)); } }}
      role="button"
      aria-expanded={isActiveDetail}
      aria-label="View combined alpha"
    >
      <td className="text-[10px] text-[#111111] dark:text-foreground font-sans font-semibold tabular-nums px-1.5 py-0.5">batch</td>
      <td className="px-1 py-0.5 text-center leading-[14px]">
        <button
          onClick={(e) => { e.stopPropagation(); useRunStore.getState().toggleAllAlphaSelection(); }}
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
      <MetricCells m={isMetrics!} />
      <MetricCells m={osMetrics!} />
      <PoolCorrCell corr={combined.poolCorrelation} />
    </tr>
  );
});

// Rows retain identity while other candidates arrive.
const RankRow = React.memo(function RankRow({ item, index, displayIndex }: { item: AlphaResult; index: number; displayIndex: number }) {
  const isMetrics = item.summary?.is, osMetrics = item.summary?.os;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useRunStore(s => s.selectedAlphaIndex === index);
  const isSelected = useRunStore(s => s.selectedAlphaIndexSet.has(index)); // O(1) Set lookup (js-set-map-lookups)
  // Actions read via getState() — not subscribed as selectors (rerender-defer-reads)

  if ((!isMetrics || !osMetrics) && !item.error) return null;

  return (
    <tr
      className={
        'rank-row border-b border-border/40 last:border-b-0 cursor-pointer transition-colors ' +
        (isActiveDetail
          ? 'bg-[#e0ddf5] dark:bg-[#38354d]'
          : 'bg-white dark:bg-background-tertiary hover:bg-[#f1effa] dark:hover:bg-row-hover')
      }
      onClick={() => startTransition(() => useRunStore.getState().enterDetailView(index))}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); startTransition(() => useRunStore.getState().enterDetailView(index)); } }}
      role="button"
      aria-expanded={isActiveDetail}
      aria-label={`View ${`alpha_${item.id}`}`}
      title={item.error}
    >
      {/* ID */}
      <td className="text-[10px] text-[#111111] dark:text-foreground font-sans font-semibold tabular-nums px-1.5 py-0.5">
        {String(displayIndex)}
      </td>
      {/* Selection (reserved for submit; not used by saving) */}
      <td className="px-1 py-0.5 text-center leading-[14px]">
        <button
          onClick={(e) => { e.stopPropagation(); useRunStore.getState().toggleAlphaSelection(index); }}
          onKeyDown={(e) => e.stopPropagation()}
          aria-pressed={isSelected}
          className="w-3 h-3 rounded-full border border-[#777777] dark:border-foreground-muted hover:border-accent transition-colors inline-flex align-middle items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
          title={isSelected ? 'Deselect' : 'Select'}
          aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
        >
          {isSelected ? <span className="w-1.5 h-1.5 rounded-full bg-foreground-secondary" /> : null}
        </button>
      </td>
      {item.error ? <td colSpan={19} className="text-[10px] text-metric-negative px-2 py-0.5">Failed: {item.error}</td> : <><MetricCells m={isMetrics!} /><MetricCells m={osMetrics!} /><PoolCorrCell corr={item.poolCorrelation} /></>}
    </tr>
  );
});

export default function ResultsTable() {
  const selectedCount = useRunStore(s => s.selectedAlphaIndices.length);
  const hasCombined = useRunStore(s => s.result?.combined != null || !!s.gaCombinedKey);
  const rows = useRunStore(visibleCandidateRows);

  // clearAlphaSelection read via getState() — not subscribed as selector (rerender-defer-reads)


  return (
    <div className="bg-background-tertiary border-b border-border flex flex-col min-h-0">
      {selectedCount > 0 ? (
        <div className="flex items-center gap-2 px-2 py-0.5 border-b border-border bg-background-secondary text-[10px]">
          <span className="text-accent">{selectedCount} selected</span>
          <button onClick={() => useRunStore.getState().clearAlphaSelection()} className="text-foreground-muted hover:text-foreground underline focus-visible:ring-2 focus-visible:ring-accent">clear</button>
        </div>
      ) : null}

      {/* Reserve the two header rows plus five compact result rows, even for a single alpha. */}
      <div className="min-h-[132px] overflow-auto pr-6 bg-white dark:bg-background-tertiary">
        <table aria-label="Alpha results" className="w-full border-collapse whitespace-nowrap text-[10px] leading-[14px] text-[#111111] dark:text-foreground">
          {TABLE_HEADER}
          <tbody>
            {hasCombined ? <BatchRow /> : null}
            {rows.map(({ alpha: item, index }, displayIndex) => (
              <RankRow key={item.id} item={item} index={index} displayIndex={displayIndex} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
