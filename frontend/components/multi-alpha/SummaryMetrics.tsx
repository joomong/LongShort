'use client';

import { useAlphaStore } from '@/store/alphaStore';
import { getMetricColor } from '@/lib/metricColors';

const formatPnl = (value: number | undefined) => {
  const v = value ?? 0;
  const sign = v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(1)}%`;
};

const formatNumber = (value: number | undefined) => {
  const v = value ?? 0;
  return v.toFixed(2);
};

export default function SummaryMetrics() {
  const result = useAlphaStore(s => s.result);
  const combined = result?.combined;

  if (!combined) return null;

  const { isMetrics, osMetrics } = combined;

  return (
    <div className="bg-background-tertiary border-b border-border">
      <div className="px-4 py-1 border-b border-border">
        <span className="text-xs text-foreground-muted">Combined Portfolio</span>
      </div>
      <div className="px-4 py-2 grid grid-cols-2 gap-x-6 gap-y-1">
        {/* IS Section */}
        <div>
          <div className="text-[10px] text-foreground-muted mb-1">IS</div>
          <div className="grid grid-cols-4 gap-x-3 gap-y-0.5">
            <div className="text-[10px] text-foreground-muted">PnL</div>
            <div className="text-[10px] text-foreground-muted">Sharpe</div>
            <div className="text-[10px] text-foreground-muted">MDD</div>
            <div className="text-[10px] text-foreground-muted">Turnover</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('pnl', isMetrics.pnl)}`}>{formatPnl(isMetrics.pnl)}</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('sharpe', isMetrics.sharpe)}`}>{formatNumber(isMetrics.sharpe)}</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('mdd', isMetrics.mdd ?? 0)}`}>{formatPnl(isMetrics.mdd)}</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('turnover', isMetrics.turnover ?? 0)}`}>{(isMetrics.turnover ?? 0).toFixed(0)}</div>
          </div>
        </div>
        {/* OS Section */}
        <div>
          <div className="text-[10px] text-foreground-muted mb-1">OS</div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
            <div className="text-[10px] text-foreground-muted">PnL</div>
            <div className="text-[10px] text-foreground-muted">Sharpe</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('pnl', osMetrics.pnl)}`}>{formatPnl(osMetrics.pnl)}</div>
            <div className={`text-xs font-mono tabular-nums ${getMetricColor('sharpe', osMetrics.sharpe)}`}>{formatNumber(osMetrics.sharpe)}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
