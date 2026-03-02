'use client';

import React from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { getMetricCellColor } from '@/lib/metricColors';

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

export default function SummaryMetrics() {
  const result = useAlphaStore(s => s.result);
  const combined = result?.combined;

  if (!combined) return null;

  const { isMetrics, osMetrics } = combined;

  return (
    <div className="bg-background-tertiary border-b border-border panel-inset">
      <div className="px-4 py-1 border-b border-border">
        <span className="text-xs text-foreground-muted">Combined Portfolio</span>
      </div>
      <div className="px-4 py-2 grid grid-cols-2 gap-x-6 gap-y-1">
        {/* IS Section */}
        <div>
          <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted mb-1">IS</div>
          <div className="grid grid-cols-3 gap-x-3 gap-y-0.5">
            <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted">Return</div>
            <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted">Sharpe</div>
            <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted">Turnover</div>
            <div className={`text-xs font-mono tabular-nums px-1 ${metricClass('pnl', isMetrics.ann_ret ?? isMetrics.pnl)}`}>{formatPnl(isMetrics.ann_ret ?? isMetrics.pnl)}</div>
            <div className={`text-xs font-mono tabular-nums px-1 ${metricClass('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</div>
            <div className={`text-xs font-mono tabular-nums px-1 ${metricClass('turnover', isMetrics.turnover)}`}>{isFiniteNumber(isMetrics.turnover) ? isMetrics.turnover.toFixed(1) : '-'}</div>
          </div>
        </div>
        {/* OS Section */}
        <div>
          <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted mb-1">OS</div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
            <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted">Return</div>
            <div className="text-[11px] uppercase tracking-wider font-medium text-foreground-muted">Sharpe</div>
            <div className={`text-xs font-mono tabular-nums px-1 ${metricClass('pnl', osMetrics.ann_ret ?? osMetrics.pnl)}`}>{formatPnl(osMetrics.ann_ret ?? osMetrics.pnl)}</div>
            <div className={`text-xs font-mono tabular-nums px-1 ${metricClass('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
