'use client';

import { useMemo } from 'react';
import {
  LineChart,
  Line,
  ResponsiveContainer,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
} from 'recharts';
import { useThemeStore } from '@/store/themeStore';
import { useAlphaStore } from '@/store/alphaStore';
import { getThemeColors } from '@/lib/themeColors';
import type { BacktestAlphaResult } from '@/types';

export type DetailMetricKey = 'pnl' | 'mdd' | 'minBet' | 'maxWeight' | 'nonZero';

interface SingleSeriesPoint {
  date: string;
  value: number;
}

export interface PnlSeriesPoint {
  date: string;
  net: number;
  gross: number;
}

const DEFAULT_CAPITAL = 100000;

export function seriesToChartData(
  series: Record<string, number | null> | null | undefined
): SingleSeriesPoint[] {
  if (!series) return [];
  return Object.entries(series)
    .sort(([a], [b]) => a.localeCompare(b))
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .map(([date, value]) => ({ date, value: value as number }));
}

export function buildPnlBundleFromRet(
  retSeries: Record<string, number | null> | null | undefined,
  turnoverSeries: Record<string, number | null> | null | undefined,
  capital: number,
  transactionFeeRate: number,
): { pnlData: PnlSeriesPoint[]; netEquityData: SingleSeriesPoint[] } {
  if (!retSeries) return { pnlData: [], netEquityData: [] };

  const retEntries = Object.entries(retSeries)
    .filter(([, r]) => typeof r === 'number' && Number.isFinite(r))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, r]) => [date, r as number] as const);

  const turnoverMap = new Map<string, number | null>();
  if (turnoverSeries) {
    for (const [d, v] of Object.entries(turnoverSeries)) {
      turnoverMap.set(d, typeof v === 'number' && Number.isFinite(v) ? v : null);
    }
  }

  let netEquity = capital;
  let grossEquity = capital;
  let grossKnown = true;
  const pnlData: PnlSeriesPoint[] = [];
  const netEquityData: SingleSeriesPoint[] = [];

  for (const [date, r] of retEntries) {
    const to = turnoverMap.get(date);
    const grossRet =
      grossKnown && typeof to === 'number'
        ? r + to * transactionFeeRate
        : Number.NaN;

    netEquity *= 1 + r;
    if (Number.isFinite(grossRet)) {
      grossEquity *= 1 + grossRet;
    } else {
      grossKnown = false;
    }

    pnlData.push({
      date,
      net: netEquity - capital,
      gross: grossKnown ? grossEquity - capital : Number.NaN,
    });
    netEquityData.push({ date, value: netEquity });
  }

  return { pnlData, netEquityData };
}

export function buildMddDataFromEquity(
  netEquityData: SingleSeriesPoint[],
): SingleSeriesPoint[] {
  if (netEquityData.length === 0) return [];
  let peak = -Infinity;
  return netEquityData.map(({ date, value }) => {
    if (value > peak) peak = value;
    const dd = (value / peak - 1) * 100;
    return { date, value: dd };
  });
}

function BigChart({
  title,
  data,
  color,
  formatValue,
}: {
  title: string;
  data: SingleSeriesPoint[];
  color: string;
  formatValue?: (v: number) => string;
}) {
  const fmt = formatValue ?? ((v: number) => v.toFixed(4));
  const { theme } = useThemeStore();
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => {
    void theme;
    return getThemeColors();
  }, [theme]);

  const monthlyTicks = useMemo(() => {
    if (data.length === 0) return [];
    const indices: number[] = [];
    let lastMonth = '';
    data.forEach((d, i) => {
      const month = d.date.slice(0, 7);
      if (month !== lastMonth) {
        indices.push(i);
        lastMonth = month;
      }
    });
    return indices;
  }, [data]);

  if (data.length === 0) {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-2">
          <span className="text-xs text-foreground-muted">{title}</span>
        </div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <div className="px-3 py-2">
        <span className="text-xs text-foreground-muted">{title}</span>
      </div>
      <div className="flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 10, bottom: 20, left: 10 }}>
            <XAxis
              dataKey="date"
              ticks={monthlyTicks.map((i) => data[i]?.date).filter(Boolean)}
              tickFormatter={(d: string) => d.slice(0, 7)}
              tick={{ fontSize: 10, fill: colors.chartText }}
              axisLine={false}
              tickLine={false}
              interval={0}
              angle={-45}
              textAnchor="end"
              height={30}
            />
            <YAxis hide domain={['auto', 'auto']} />
            <Tooltip
              contentStyle={{
                backgroundColor: colors.backgroundElevated,
                border: `1px solid ${colors.chartGrid}`,
                borderRadius: 0,
                fontSize: 12,
                color: colors.chartText,
                boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              }}
              labelStyle={{ color: colors.chartText, fontSize: 11 }}
              formatter={(value) => {
                const n = typeof value === 'number' ? value : Number(value);
                return [Number.isFinite(n) ? fmt(n) : 'N/A', title];
              }}
              labelFormatter={(_label, payload) => {
                const entry = payload?.[0]?.payload as SingleSeriesPoint | undefined;
                return entry?.date ?? '';
              }}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke={color}
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function PnlDualChart({ data }: { data: PnlSeriesPoint[] }) {
  const { theme } = useThemeStore();
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => {
    void theme;
    return getThemeColors();
  }, [theme]);

  const monthlyTicks = useMemo(() => {
    if (data.length === 0) return [];
    const indices: number[] = [];
    let lastMonth = '';
    data.forEach((d, i) => {
      const month = d.date.slice(0, 7);
      if (month !== lastMonth) {
        indices.push(i);
        lastMonth = month;
      }
    });
    return indices;
  }, [data]);

  if (data.length === 0) {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-2">
          <span className="text-xs text-foreground-muted">PnL ($)</span>
        </div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  const formatUsd = (v: number) =>
    `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  return (
    <div className="h-full flex flex-col">
      <div className="px-3 py-2">
        <span className="text-xs text-foreground-muted">PnL ($)</span>
      </div>
      <div className="flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 10, bottom: 20, left: 10 }}>
            <XAxis
              dataKey="date"
              ticks={monthlyTicks.map((i) => data[i]?.date).filter(Boolean)}
              tickFormatter={(d: string) => d.slice(0, 7)}
              tick={{ fontSize: 10, fill: colors.chartText }}
              axisLine={false}
              tickLine={false}
              interval={0}
              angle={-45}
              textAnchor="end"
              height={30}
            />
            <YAxis hide domain={['auto', 'auto']} />
            <Tooltip
              contentStyle={{
                backgroundColor: colors.backgroundElevated,
                border: `1px solid ${colors.chartGrid}`,
                borderRadius: 0,
                fontSize: 12,
                color: colors.chartText,
                boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              }}
              labelStyle={{ color: colors.chartText, fontSize: 11 }}
              formatter={(value: unknown, name: unknown) => {
                const n = typeof value === 'number' ? value : Number(value);
                return [
                  Number.isFinite(n) ? formatUsd(n) : 'N/A',
                  name === 'net' ? 'Fee Included' : 'No Fee',
                ];
              }}
              labelFormatter={(_label, payload) => {
                const entry = payload?.[0]?.payload as PnlSeriesPoint | undefined;
                return entry?.date ?? '';
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11 }}
              formatter={(value) =>
                value === 'net' ? 'Fee Included (10bps RT)' : 'No Fee'
              }
            />
            <Line
              type="monotone"
              dataKey="net"
              stroke="#4af6c3"
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="gross"
              stroke="#ff9800"
              strokeDasharray="4 2"
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export default function DetailTimeSeries({
  alpha,
  metric,
  initialCapital,
}: {
  alpha: BacktestAlphaResult;
  metric: DetailMetricKey;
  initialCapital?: number;
}) {
  const transactionFeeRate = useAlphaStore(
    (s) => s.dataRange?.transactionFeeRate ?? 0.001
  );
  const capital = initialCapital ?? DEFAULT_CAPITAL;

  const pnlBundle = useMemo(() => {
    const ret = alpha.isRetSeries;
    const turnover = alpha.isTurnoverSeries ?? null;

    if (ret) {
      return buildPnlBundleFromRet(ret, turnover, capital, transactionFeeRate);
    }

    // Fallback when return series is missing.
    const eq = alpha.isEquitySeries;
    if (!eq) return { pnlData: [], netEquityData: [] };
    const entries = Object.entries(eq)
      .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, value]) => [date, value as number] as const);
    const pnlData: PnlSeriesPoint[] = entries.map(([date, value]) => ({
      date,
      net: value - capital,
      gross: Number.NaN,
    }));
    const netEquityData: SingleSeriesPoint[] = entries.map(([date, value]) => ({
      date,
      value,
    }));
    return { pnlData, netEquityData };
  }, [alpha.isRetSeries, alpha.isTurnoverSeries, alpha.isEquitySeries, capital, transactionFeeRate]);

  const mddData = useMemo(() => {
    return buildMddDataFromEquity(pnlBundle.netEquityData);
  }, [pnlBundle.netEquityData]);

  const minBetData = useMemo(
    () => seriesToChartData(alpha.isMinBetSeries),
    [alpha.isMinBetSeries]
  );
  const maxWeightData = useMemo(
    () => seriesToChartData(alpha.isMaxWeightSeries),
    [alpha.isMaxWeightSeries]
  );
  const nonZeroData = useMemo(
    () => seriesToChartData(alpha.isNonzeroPositionSeries),
    [alpha.isNonzeroPositionSeries]
  );

  if (metric === 'pnl') {
    return <PnlDualChart data={pnlBundle.pnlData} />;
  }

  if (metric === 'mdd') {
    return (
      <BigChart
        title="MDD (%)"
        data={mddData}
        color="#ff433d"
        formatValue={(v) => `${v.toFixed(2)}%`}
      />
    );
  }

  if (metric === 'minBet') {
    return (
      <BigChart
        title="Min Bet"
        data={minBetData}
        color="#0078ff"
        formatValue={(v) => v.toFixed(4)}
      />
    );
  }

  if (metric === 'maxWeight') {
    return (
      <BigChart
        title="Max Weight"
        data={maxWeightData}
        color="#ff9800"
        formatValue={(v) => `${(v * 100).toFixed(2)}%`}
      />
    );
  }

  return (
    <BigChart
      title="Non-zero Position"
      data={nonZeroData}
      color="#0078ff"
      formatValue={(v) => `${v.toFixed(0)}`}
    />
  );
}
