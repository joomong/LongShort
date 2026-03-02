'use client';

import React from 'react';
import type { BacktestAlphaResult } from '@/types';
import { getSignCellColor, getMetricCellColor } from '@/lib/metricColors';

export default function DetailSummaryTable({ alpha }: { alpha: BacktestAlphaResult }) {
  const yearly = alpha.yearly;

  if (!yearly || yearly.length === 0) {
    return (
      <div className="border-t border-border px-3 py-3 text-sm text-foreground-muted">
        No yearly summary (missing yearly data from engine)
      </div>
    );
  }

  return (
    <div>
      <div className="px-3 py-2 text-[11px] text-foreground-muted border-b border-border">
        Yearly Table
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="text-xs uppercase tracking-wider font-medium text-foreground-secondary border-b border-border-strong">
              <th className="text-left px-3 py-1.5">Year</th>
              <th className="text-right px-3 py-1.5">Long SM</th>
              <th className="text-right px-3 py-1.5">Short SM</th>
              <th className="text-right px-3 py-1.5">PnL</th>
              <th className="text-right px-3 py-1.5">Ann.Net</th>
              <th className="text-right px-3 py-1.5">Sharpe(f)</th>
              <th className="text-right px-3 py-1.5">Max DD</th>
              <th className="text-right px-3 py-1.5">Daily TVR</th>
              <th className="text-right px-3 py-1.5">Margin</th>
            </tr>
          </thead>
          <tbody>
            {yearly.map((r) => (
              <tr key={r.year} className="border-b border-border/40 last:border-b-0 hover:bg-row-hover">
                <td className="px-3 py-1.5 font-mono tabular-nums text-foreground-muted">{r.year}</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{(r.longSM * 100).toFixed(1)}%</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{(r.shortSM * 100).toFixed(1)}%</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getSignCellColor(r.pnl)}`}>{r.pnl >= 0 ? '+' : ''}{r.pnl.toFixed(1)}%</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getSignCellColor(r.annRet)}`}>{r.annRet >= 0 ? '+' : ''}{r.annRet.toFixed(1)}%</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getMetricCellColor('sharpe', r.sharpe)}`}>{r.sharpe.toFixed(2)}</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getMetricCellColor('mdd', r.maxDD)}`}>{r.maxDD.toFixed(1)}%</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{(r.dailyTvr * 100).toFixed(1)}</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{r.margin.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
