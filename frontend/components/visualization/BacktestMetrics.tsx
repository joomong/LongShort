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

function ResultRow({ item, index }: { item: BacktestAlphaResult; index: number }) {
  const { isMetrics, osMetrics } = item;
  // Granular selectors — only re-render when this row's state changes
  const isActiveDetail = useAlphaStore(s => s.selectedAlphaIndex === index);
  const isSelected = useAlphaStore(s => s.selectedAlphaIndices.includes(index));
  const enterDetailView = useAlphaStore(s => s.enterDetailView);
  const toggleAlphaSelection = useAlphaStore(s => s.toggleAlphaSelection);

  if (!isMetrics || !osMetrics) return null;

  const cellClass = "text-xs font-mono tabular-nums text-right px-3 py-1.5 text-data-color";

  return (
    <tr
      className={
        'border-b border-border/40 cursor-pointer transition-colors ' +
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
        <button
          onClick={(e) => { e.stopPropagation(); toggleAlphaSelection(index); }}
          className="w-3.5 h-3.5 border border-foreground-muted/40 hover:border-accent transition-colors inline-flex items-center justify-center focus-visible:ring-2 focus-visible:ring-accent"
          title={isSelected ? 'Deselect' : 'Select'}
          aria-label={isSelected ? 'Deselect alpha' : 'Select alpha'}
        >
          {isSelected && <span className="w-2 h-2 bg-accent" />}
        </button>
      </td>
      {/* Index */}
      <td className="text-xs text-foreground-muted font-mono tabular-nums px-3 py-1.5">#{index + 1}</td>

      {/* IS Metrics */}
      <td className={`${cellClass} ${metricClass('pnl', isMetrics.cagr ?? isMetrics.pnl)}`}>{formatPnl(isMetrics.cagr ?? isMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('mdd', isMetrics.mdd)}`}>{formatPnl(isMetrics.mdd)}</td>
      <td className={`${cellClass} ${metricClass('turnover', isMetrics.turnover)}`}>{isFiniteNumber(isMetrics.turnover) ? isMetrics.turnover.toFixed(0) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.min_bet) ? isMetrics.min_bet.toFixed(0) : '-'}</td>
      <td className={cellClass}>{isFiniteNumber(isMetrics.max_weight) ? `${(isMetrics.max_weight * 100).toFixed(1)}%` : '-'}</td>
      <td className={`${cellClass} ${metricClass('ir', isMetrics.ir)}`}>{formatNumber(isMetrics.ir)}</td>
      <td className={`${cellClass} ${metricClass('corr', isMetrics.corr)}`}>{formatNumber(isMetrics.corr)}</td>

      {/* OS Metrics — thicker left border for group separator */}
      <td className={`${cellClass} border-l border-border-strong ${metricClass('pnl', osMetrics.cagr ?? osMetrics.pnl)}`}>{formatPnl(osMetrics.cagr ?? osMetrics.pnl)}</td>
      <td className={`${cellClass} ${metricClass('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</td>
      <td className={`${cellClass} ${metricClass('mdd', osMetrics.mdd)}`}>{formatPnl(osMetrics.mdd)}</td>
      <td className={`${cellClass} ${metricClass('ir', osMetrics.ir)}`}>{formatNumber(osMetrics.ir)}</td>
    </tr>
  );
}

export default function BacktestMetrics() {
  const alphas = useAlphaStore(s => s.result?.alphas) ?? EMPTY_ALPHAS;

  return (
    <div className="bg-background-tertiary border-b border-border panel-inset">
      {/* Scrollable Results Table */}
      <div className="h-32 overflow-y-auto">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-background-secondary/50">
            {/* Group Header Row */}
            <tr className="text-[11px] uppercase tracking-wider font-semibold text-foreground-secondary border-b-2 border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10">#</th>
              <th className="text-center py-0.5" colSpan={8}>IS</th>
              <th className="text-center py-0.5 border-l border-border-strong" colSpan={4}>OS</th>
            </tr>
            {/* Metric Header Row */}
            <tr className="text-xs font-medium text-foreground-secondary border-b border-border-strong">
              <th className="w-6"></th>
              <th className="text-left w-10 px-2.5">#</th>
              <th className="text-right px-2.5">CAGR</th>
              <th className="text-right px-2.5">Sharpe</th>
              <th className="text-right px-2.5">MDD</th>
              <th className="text-right px-2.5">Turn</th>
              <th className="text-right px-2.5">MinBet</th>
              <th className="text-right px-2.5">MaxWt</th>
              <th className="text-right px-2.5">IR</th>
              <th className="text-right px-2.5">Corr</th>
              <th className="text-right px-2.5 border-l border-border-strong">CAGR</th>
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
