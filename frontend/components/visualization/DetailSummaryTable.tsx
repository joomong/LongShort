'use client';

import React from 'react';
import { useMemo } from 'react';
import type { BacktestAlphaResult } from '@/types';
import { useAlphaStore } from '@/store/alphaStore';
import { getSignCellColor, getMetricCellColor } from '@/lib/metricColors';

const DEFAULT_CAPITAL = 100000;
const ANNUALIZATION = 365; // crypto markets trade 24/7

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

    const retEntries = Object.entries(retSeries)
      .filter(([, r]) => typeof r === 'number' && Number.isFinite(r))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, r]) => [date, r as number] as const);
    const byYear = new Map<string, Array<[string, number]>>();
    for (const [date, r] of retEntries) {
      const y = toYear(date);
      if (!byYear.has(y)) byYear.set(y, []);
      byYear.get(y)!.push([date, r]);
    }

    const turnoverByDate = new Map<string, number | null>();
    const turnoverSeries = alpha.isTurnoverSeries ?? null;
    if (turnoverSeries) {
      for (const [date, v] of Object.entries(turnoverSeries)) {
        turnoverByDate.set(
          date,
          typeof v === 'number' && Number.isFinite(v) ? v : null
        );
      }
    }

    const longPct = (alpha.longRatio ?? 0) * 100;
    const shortPct = (alpha.shortRatio ?? 0) * 100;

    return Array.from(byYear.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([year, entries]) => {
        const netReturns = entries.map(([, r]) => r);
        let grossAvailable = true;
        const grossReturns: number[] = [];
        for (const [date, r] of entries) {
          const to = turnoverByDate.get(date);
          if (typeof to !== 'number') {
            grossAvailable = false;
            break;
          }
          grossReturns.push(r + to * transactionFeeRate);
        }

        const netEquity = netReturns.reduce((e, r) => e * (1 + r), 1);
        const grossEquity = grossAvailable
          ? grossReturns.reduce((e, r) => e * (1 + r), 1)
          : null;
        const pnlNetPct = (netEquity - 1) * 100;
        const pnlGrossPct = grossEquity !== null ? (grossEquity - 1) * 100 : null;

        const mean = netReturns.length ? netReturns.reduce((a, b) => a + b, 0) / netReturns.length : 0;
        const std = safeStd(netReturns);
        const sharpe = std === 0 ? 0 : (mean / std) * Math.sqrt(ANNUALIZATION);

        const mddPct = computeYearMddPct(entries);
        const feeImpactUsd =
          grossEquity !== null ? (grossEquity - netEquity) * capital : null;
        const feeImpactPct =
          pnlGrossPct !== null ? pnlGrossPct - pnlNetPct : null;

        return {
          year,
          pnlNetPct,
          pnlGrossPct,
          sharpe,
          mddPct,
          longPct,
          shortPct,
          feeImpactUsd,
          feeImpactPct,
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
            <tr className="text-xs uppercase tracking-wider font-medium text-foreground-secondary border-b border-border-strong">
              <th className="text-left px-3 py-1.5">Year</th>
              <th className="text-right px-3 py-1.5">PnL (Net)</th>
              <th className="text-right px-3 py-1.5">PnL (No Fee)</th>
              <th className="text-right px-3 py-1.5">Sharpe</th>
              <th className="text-right px-3 py-1.5">MDD</th>
              <th className="text-right px-3 py-1.5">Long</th>
              <th className="text-right px-3 py-1.5">Short</th>
              <th className="text-right px-3 py-1.5">Fee Impact</th>
            </tr>
          </thead>
          <tbody>
            {yearlyRows.map((r) => (
              <tr key={r.year} className="border-b border-border/40 last:border-b-0 hover:bg-row-hover">
                <td className="px-3 py-1.5 font-mono tabular-nums text-foreground-muted">{r.year}</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getSignCellColor(r.pnlNetPct)}`}>{r.pnlNetPct >= 0 ? '+' : ''}{r.pnlNetPct.toFixed(1)}%</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${r.pnlGrossPct !== null ? getSignCellColor(r.pnlGrossPct) : 'text-foreground-muted'}`}>
                  {r.pnlGrossPct === null ? '-' : `${r.pnlGrossPct >= 0 ? '+' : ''}${r.pnlGrossPct.toFixed(1)}%`}
                </td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getMetricCellColor('sharpe', r.sharpe)}`}>{r.sharpe.toFixed(2)}</td>
                <td className={`px-3 py-1.5 text-right font-mono tabular-nums ${getMetricCellColor('mdd', r.mddPct)}`}>{r.mddPct.toFixed(1)}%</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{r.longPct.toFixed(1)}%</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">{r.shortPct.toFixed(1)}%</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-data-color">
                  {r.feeImpactPct === null || r.feeImpactUsd === null
                    ? '-'
                    : `+${r.feeImpactPct.toFixed(2)}% / $${r.feeImpactUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
