'use client';

import React from 'react';
import type { BacktestAlphaResult } from '@/types';
import { REPORT_BOOK_M } from '@/lib/reportUnits';

export default function DetailSummaryTable({ alpha }: { alpha: BacktestAlphaResult }) {
  const yearly = alpha.yearly;

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
              <th className="text-left px-1.5 py-0.5">year</th>
              <th className="text-right px-1.5 py-0.5">long $M</th>
              <th className="text-right px-1.5 py-0.5">short $M</th>
              <th className="text-right px-1.5 py-0.5" title="Fixed $20 million reporting book">bks $M</th>
              <th className="text-right px-1.5 py-0.5">pnl $M</th>
              <th className="text-right px-1.5 py-0.5">ann.ret</th>
              <th className="text-right px-1.5 py-0.5">sharpe[f]</th>
              <th className="text-right px-1.5 py-0.5">max.dd</th>
              <th className="text-right px-1.5 py-0.5" title="Bars with positive return / valid return bars">win/lose</th>
              <th className="text-right px-1.5 py-0.5">daily tvr</th>
              <th className="text-right px-1.5 py-0.5">margin</th>
              <th className="text-right px-1.5 py-0.5">fitness</th>
            </tr>
          </thead>
          <tbody>
            {[...yearly].sort((a, b) => b.year - a.year).map((r) => (
              <tr key={r.year} className="hover:bg-row-hover">
                <td className="px-1.5 py-0.5 font-sans tabular-nums text-data-color">{r.year}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{(r.longSM * REPORT_BOOK_M).toFixed(2)}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{(-Math.abs(r.shortSM) * REPORT_BOOK_M).toFixed(2)}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{REPORT_BOOK_M.toFixed(1)}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{(r.pnl / 100 * REPORT_BOOK_M).toFixed(2)}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{r.annRet.toFixed(2)}%</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{r.sharpe.toFixed(3)}<span title="Bracketed metric definition not yet specified">[—]</span></td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{r.maxDD.toFixed(2)}%</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{typeof r.winLose === 'number' && Number.isFinite(r.winLose) ? `${r.winLose.toFixed(2)}%` : '—'}</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{(r.dailyTvr * 100).toFixed(2)}%</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color">{(r.margin * 10000).toFixed(2)} bps</td>
                <td className="px-1.5 py-0.5 text-right font-sans tabular-nums text-data-color" title="Fitness definition not yet specified">—</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
