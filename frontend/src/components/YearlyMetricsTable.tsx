'use client';

import React from 'react';
import type { AlphaResult, RunContext } from '@/types';
import { formatPercent, MILLION_DOLLARS } from '@/format';

function YearlyMetricsTable({ alpha, context }: { alpha: AlphaResult; context: RunContext }) {
  const yearly = alpha.yearly;
  const reportBookM = context.reportNotional / MILLION_DOLLARS;

  if (!yearly || yearly.length === 0) {
    return (
      <div className="border-t border-border px-3 py-3 text-sm text-data-color">
        No yearly summary (missing yearly data from engine)
      </div>
    );
  }

  return (
    <div>
      <div className="overflow-x-auto">
        <table aria-label="Yearly performance" className="w-full text-[11px] font-sans border-collapse whitespace-nowrap text-data-color">
          <thead>
            <tr className="text-[10px] font-semibold text-data-color">
              <th className="text-left px-1.5 py-0 leading-[18px]">year</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">long $M</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">short $M</th>
              <th className="text-right px-1.5 py-0 leading-[18px]" title="Reporting notional from the execution context; gross exposure = 1">bks $M</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">pnl $M</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">ann.ret</th>
              <th className="text-right px-1.5 py-0 leading-[18px]" title="Annualized Sharpe [daily mean gross return / sample standard deviation]">sharpe[ir]</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">max.dd</th>
              <th className="text-right px-1.5 py-0 leading-[18px]" title="Bars with positive return / valid return bars">win/lose</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">daily tvr</th>
              <th className="text-right px-1.5 py-0 leading-[18px]">margin</th>
              <th className="text-right px-1.5 py-0 leading-[18px]" title="Sharpe × sqrt(abs(annual return) / max(daily turnover, 0.125)); full gross book, leverage 1">fitness</th>
            </tr>
          </thead>
          <tbody>
            {[...yearly].sort((a, b) => b.year - a.year).map((r) => (
              <tr key={r.year} className="hover:bg-row-hover">
                <td className="px-1.5 py-0 leading-[18px] font-sans tabular-nums text-data-color">{r.year}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{(r.longRatio * reportBookM).toFixed(2)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{(-Math.abs(r.shortRatio) * reportBookM).toFixed(2)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{reportBookM.toFixed(1)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{(r.pnl * reportBookM).toFixed(2)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{formatPercent(r.annualReturn)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{r.sharpe.toFixed(3)}<span title="IR = daily mean gross return / sample standard deviation, without annualization">[{typeof r.informationRatio === 'number' && Number.isFinite(r.informationRatio) ? r.informationRatio.toFixed(3) : '—'}]</span></td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{formatPercent(r.maxDrawdown)}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{typeof r.winRate === 'number' && Number.isFinite(r.winRate) ? formatPercent(r.winRate) : '—'}</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{(r.turnover * 100).toFixed(2)}%</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{(r.margin * 10000).toFixed(2)} bps</td>
                <td className="px-1.5 py-0 leading-[18px] text-right font-sans tabular-nums text-data-color">{typeof r.fitness === 'number' && Number.isFinite(r.fitness) ? r.fitness.toFixed(3) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default React.memo(YearlyMetricsTable);
