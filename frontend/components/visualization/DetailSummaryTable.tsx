'use client';

import { useMemo } from 'react';
import type { BacktestAlphaResult } from '@/types';
import { useAlphaStore } from '@/store/alphaStore';

const DEFAULT_CAPITAL = 100000;
const ANNUALIZATION = 252; // approximation (D0/D1 모두 일단 252로 고정)

function toYear(date: string): string {
  // expecting YYYY-MM-DD
  return date.slice(0, 4);
}

function safeStd(values: number[]): number {
  if (values.length < 2) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const varSum = values.reduce((s, v) => s + (v - mean) * (v - mean), 0);
  return Math.sqrt(varSum / (values.length - 1));
}

function computeYearMddPct(returns: Array<[string, number]>): number {
  let equity = 1;
  let peak = 1;
  let minDd = 0; // negative
  for (const [, r] of returns) {
    equity *= (1 + r);
    if (equity > peak) peak = equity;
    const dd = equity / peak - 1;
    if (dd < minDd) minDd = dd;
  }
  return minDd * 100;
}

export default function DetailSummaryTable({ alpha, initialCapital }: { alpha: BacktestAlphaResult; initialCapital?: number }) {
  const capital = initialCapital ?? DEFAULT_CAPITAL;
  const transactionFeeRate = useAlphaStore((s) => s.dataRange?.transactionFeeRate ?? 0.001);
  const yearlyRows = useMemo(() => {
    const retSeries = alpha.isRetSeries;
    if (!retSeries) return [];

    const retEntries = Object.entries(retSeries).sort(([a], [b]) => a.localeCompare(b));
    const byYear = new Map<string, Array<[string, number]>>();
    for (const [date, r] of retEntries) {
      const y = toYear(date);
      if (!byYear.has(y)) byYear.set(y, []);
      byYear.get(y)!.push([date, r]);
    }

    // turnover series (optional)
    const turnoverSeries = alpha.isTurnoverSeries ?? null;
    const turnoverByYear = new Map<string, number[]>();
    if (turnoverSeries) {
      for (const [date, v] of Object.entries(turnoverSeries)) {
        const y = toYear(date);
        if (!turnoverByYear.has(y)) turnoverByYear.set(y, []);
        turnoverByYear.get(y)!.push(v);
      }
    }

    const longPct = (alpha.longRatio ?? 0) * 100;
    const shortPct = (alpha.shortRatio ?? 0) * 100;

    return Array.from(byYear.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([year, entries]) => {
        const returns = entries.map(([, r]) => r);
        const equity = returns.reduce((e, r) => e * (1 + r), 1);
        const pnlPct = (equity - 1) * 100;

        const mean = returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : 0;
        const std = safeStd(returns);
        const sharpe = std === 0 ? 0 : (mean / std) * Math.sqrt(ANNUALIZATION);

        const mddPct = computeYearMddPct(entries);

        // fee estimate from turnover (if present); otherwise fallback to isMetrics.turnover
        const tvals = turnoverByYear.get(year) ?? [];
        let avgTurnoverPct: number;
        if (tvals.length) {
          const avg = tvals.reduce((a, b) => a + b, 0) / tvals.length;
          // heuristic: if avg <= 1, treat as ratio; else as percent
          avgTurnoverPct = avg <= 1 ? avg * 100 : avg;
        } else {
          avgTurnoverPct = alpha.isMetrics.turnover ?? 0;
        }
        const tradingDays = returns.length;
        const fee = (avgTurnoverPct / 100) * transactionFeeRate * tradingDays * capital;

        return {
          year,
          pnlPct,
          sharpe,
          mddPct,
          longPct,
          shortPct,
          fee,
        };
      });
  }, [alpha, capital, transactionFeeRate]);

  if (yearlyRows.length === 0) {
    return (
      <div className="border-t border-border px-3 py-3 text-sm text-foreground-muted">
        No yearly summary (missing IS return series)
      </div>
    );
  }

  return (
    <div>
      <div className="px-3 py-2 text-[11px] text-foreground-muted border-b border-border">
        1Y Table (calendar year)
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="text-[11px] font-medium text-foreground-secondary border-b border-border-strong">
              <th className="text-left px-2.5 py-1">Year</th>
              <th className="text-right px-2.5 py-1">PnL</th>
              <th className="text-right px-2.5 py-1">Sharpe</th>
              <th className="text-right px-2.5 py-1">MDD</th>
              <th className="text-right px-2.5 py-1">Long</th>
              <th className="text-right px-2.5 py-1">Short</th>
              <th className="text-right px-2.5 py-1">Fee</th>
            </tr>
          </thead>
          <tbody>
            {yearlyRows.map((r) => (
              <tr key={r.year} className="border-b border-border/40 last:border-b-0 hover:bg-row-hover">
                <td className="px-2.5 py-1 font-mono tabular-nums text-foreground-muted">{r.year}</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">{r.pnlPct >= 0 ? '+' : ''}{r.pnlPct.toFixed(1)}%</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">{r.sharpe.toFixed(2)}</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">{r.mddPct.toFixed(1)}%</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">{r.longPct.toFixed(1)}%</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">{r.shortPct.toFixed(1)}%</td>
                <td className="px-2.5 py-1 text-right font-mono tabular-nums">${r.fee.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
