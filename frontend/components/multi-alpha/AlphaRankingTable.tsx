'use client';

import React from 'react';
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

function RankRow({ item, index }: { item: BacktestAlphaResult; index: number }) {
  const { isMetrics, osMetrics } = item;
  const isCombined = item.rank === 0;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === index);
  const isSelected = useAlphaStore(s => s.selectedAlphaIndices.includes(index));
  const enterDetailView = useAlphaStore(s => s.enterDetailView);
  const toggleAlphaSelection = useAlphaStore(s => s.toggleAlphaSelection);

  if (!isMetrics || !osMetrics) return null;

  const cellClass = "text-xs font-mono tabular-nums text-right px-3 py-1.5 text-data-color";
  const formatIrCorr = (value: number | undefined | null) =>
    isCombined && value === undefined ? '-' : formatNumber(value);

  return (
    <tr
      className={
        'border-b border-border/40 last:border-b-0 cursor-pointer transition-colors ' +
        (isCombined ? 'border-b-2 border-border-strong font-semibold ' : '') +
        (isActiveDetail
          ? 'bg-row-active border-l-2 border-l-accent'
          : 'hover:bg-row-hover')
      }
      onClick={() => enterDetailView(index)}
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); enterDetailView(index); } }}
      role="button"
    >
      {/* Selection checkbox */}
      <td className="px-1 py-1 text-center">
        {!isCombined && (
          <button
            onClick={(e) => { e.stopPropagation(); toggleAlphaSelection(index); }}
            className="w-4 h-4 border border-foreground-muted/40 hover:border-accent transition-colors inline-flex items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
            title={isSelected ? 'Deselect' : 'Select'}
            aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
          >
            {isSelected && <span className="w-2.5 h-2.5 bg-accent" />}
          </button>
        )}
      </td>
      {/* Rank */}
      <td className="text-xs text-foreground-muted font-mono tabular-nums px-3 py-1.5">
        {isCombined ? '' : `#${index}`}
      </td>
      {/* Name */}
      <td className="text-xs font-mono px-3 py-1.5 truncate max-w-[100px]" title={item.name}>{item.name}</td>

      {/* IS Metrics */}
      <td className={`${cellClass} ${metricClass('pnl', isMetrics.cagr ?? isMetrics.pnl)}`}>{formatPnl(isMetrics.cagr ?? isMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('mdd', isMetrics.mdd)}`}>{formatPnl(isMetrics.mdd)}</td>
      <td className={`${cellClass} ${metricClass('turnover', isMetrics.turnover)}`}>{isFiniteNumber(isMetrics.turnover) ? isMetrics.turnover.toFixed(0) : '-'}</td>
      <td className={`${cellClass} ${metricClass('ir', isMetrics.ir)}`}>{formatIrCorr(isMetrics.ir)}</td>
      <td className={`${cellClass} ${metricClass('corr', isMetrics.corr)}`}>{formatIrCorr(isMetrics.corr)}</td>

      {/* OS Metrics */}
      <td className={`${cellClass} border-l border-border-strong ${metricClass('pnl', osMetrics.cagr ?? osMetrics.pnl)}`}>{formatPnl(osMetrics.cagr ?? osMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('ir', osMetrics.ir)}`}>{formatIrCorr(osMetrics.ir)}</td>
      <td className={`${cellClass} ${metricClass('corr', osMetrics.corr)}`}>{formatIrCorr(osMetrics.corr)}</td>
    </tr>
  );
}

export default function AlphaRankingTable() {
  const alphas = useAlphaStore(s => s.result?.alphas) ?? EMPTY_ALPHAS;
  const sortedBy = useAlphaStore(s => s.result?.sortedBy);
  const selectedCount = useAlphaStore(s => s.selectedAlphaIndices.length);
  const clearAlphaSelection = useAlphaStore(s => s.clearAlphaSelection);

  const hasCombined = alphas.length > 0 && alphas[0].rank === 0;
  const alphaCount = hasCombined ? alphas.length - 1 : alphas.length;

  if (alphas.length === 0) return null;

  return (
    <div className="bg-background-tertiary border-b border-border flex-1 flex flex-col min-h-0 panel-inset">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-1.5 border-b border-border bg-background-secondary/50">
        <span className="text-xs text-foreground-muted">
          Alpha Ranking ({alphaCount})
          {selectedCount > 0 && (
            <>
              {' '}&middot;{' '}
              <span className="text-blue-400">{selectedCount} selected</span>
              {' '}
              <button onClick={clearAlphaSelection} className="text-foreground-muted hover:text-foreground underline focus-visible:ring-2 focus-visible:ring-accent">clear</button>
            </>
          )}
        </span>
        <span className="text-xs text-foreground-muted">
          {sortedBy ? `sortedBy=${sortedBy}` : ''}
        </span>
      </div>

      {/* Scrollable Table */}
      <div className="flex-1 overflow-y-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-background-secondary/50">
            {/* Group Header Row */}
            <tr className="text-[11px] uppercase tracking-wider font-semibold text-foreground-secondary border-b-2 border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10">#</th>
              <th className="text-left w-16">Name</th>
              <th className="text-center pb-0" colSpan={6}>IS</th>
              <th className="text-center pb-0 border-l border-border-strong" colSpan={4}>OS</th>
            </tr>
            {/* Metric Header Row */}
            <tr className="text-xs font-medium text-foreground-secondary border-b border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10">#</th>
              <th className="text-left w-16">Name</th>
              <th className="text-right px-2.5">CAGR</th>
              <th className="text-right px-2.5">Sharpe</th>
              <th className="text-right px-2.5">MDD</th>
              <th className="text-right px-2.5">Turnover</th>
              <th className="text-right px-2.5">IR</th>
              <th className="text-right px-2.5">Corr</th>
              <th className="text-right px-2.5 border-l border-border-strong">CAGR</th>
              <th className="text-right px-2.5">Sharpe</th>
              <th className="text-right px-2.5">IR</th>
              <th className="text-right px-2.5">Corr</th>
            </tr>
          </thead>
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
