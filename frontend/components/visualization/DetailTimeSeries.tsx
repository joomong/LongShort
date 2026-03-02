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

export type DetailMetricKey = 'pnl' | 'turnover' | 'nonZero' | 'maxWeight' | 'mdd' | 'minBet';

interface SingleSeriesPoint {
  date: string;
  value: number;
}

export interface PnlSeriesPoint {
  date: string;
  net: number;
  gross: number;
}

interface IsOsPnlPoint {
  date: string;
  isNet?: number;
  osNet?: number;
}

interface IsOsSeriesPoint {
  date: string;
  isValue?: number;
  osValue?: number;
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

  // STAR uses additive (simple) PnL: cumRet is the running sum of daily return fractions.
  // Daily dollar PnL = cumRet * capital. Do NOT compound (multiply equity by 1+r).
  let cumRet = 0;
  let cumGrossRet = 0;
  let grossKnown = true;
  const pnlData: PnlSeriesPoint[] = [];
  const netEquityData: SingleSeriesPoint[] = [];

  for (const [date, r] of retEntries) {
    const to = turnoverMap.get(date);
    const grossRet =
      grossKnown && typeof to === 'number'
        ? r + to * transactionFeeRate
        : Number.NaN;

    cumRet += r;
    if (Number.isFinite(grossRet)) {
      cumGrossRet += grossRet;
    } else {
      grossKnown = false;
    }

    pnlData.push({
      date,
      net: cumRet * capital,
      gross: grossKnown ? cumGrossRet * capital : Number.NaN,
    });
    netEquityData.push({ date, value: capital + cumRet * capital });
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

function splitPnlByOs(
  pnlData: PnlSeriesPoint[],
  osStartDate: string | undefined
): IsOsPnlPoint[] {
  return pnlData.map((p, i, arr) => {
    const isOs = osStartDate !== undefined && p.date >= osStartDate;
    const isBoundary = isOs && i > 0 && arr[i - 1].date < osStartDate!;
    return {
      date: p.date,
      // IS 구간이거나, IS→OS 경계점에서 연결선 유지
      isNet: !isOs || isBoundary ? p.net : undefined,
      osNet: isOs ? p.net : undefined,
    };
  });
}

function mergeIsOsSeries(
  isData: SingleSeriesPoint[],
  osData: SingleSeriesPoint[]
): IsOsSeriesPoint[] {
  const merged: IsOsSeriesPoint[] = [];
  for (const p of isData) {
    merged.push({ date: p.date, isValue: p.value, osValue: undefined });
  }
  if (isData.length > 0 && osData.length > 0) {
    const lastIs = isData[isData.length - 1];
    for (const p of osData) {
      merged.push({ date: p.date, isValue: undefined, osValue: p.value });
    }
    // Overlap: first OS point also has isValue
    if (merged.length > isData.length) {
      merged[isData.length].isValue = lastIs.value;
    }
  } else {
    for (const p of osData) {
      merged.push({ date: p.date, isValue: undefined, osValue: p.value });
    }
  }
  return merged;
}

function PnlDualChart({ pnlData, osStartDate }: { pnlData: PnlSeriesPoint[]; osStartDate?: string }) {
  const data = useMemo(() => splitPnlByOs(pnlData, osStartDate), [pnlData, osStartDate]);
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
                  name === 'isNet' ? 'IS' : 'OS',
                ];
              }}
              labelFormatter={(_label, payload) => {
                const entry = payload?.[0]?.payload as IsOsPnlPoint | undefined;
                return entry?.date ?? '';
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11 }}
              formatter={(value) => (value === 'isNet' ? 'IS' : 'OS')}
            />
            <Line
              type="monotone"
              dataKey="isNet"
              stroke="#4af6c3"
              dot={false}
              strokeWidth={2}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="osNet"
              stroke="#ff9800"
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

function BigChart({
  title,
  data,
  color,
  osData,
  osColor,
  formatValue,
}: {
  title: string;
  data: SingleSeriesPoint[];
  color: string;
  osData?: SingleSeriesPoint[];
  osColor?: string;
  formatValue?: (v: number) => string;
}) {
  const fmt = formatValue ?? ((v: number) => v.toFixed(4));
  const { theme } = useThemeStore();
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => {
    void theme;
    return getThemeColors();
  }, [theme]);

  const hasOs = osData && osData.length > 0;

  const mergedData = useMemo(() => {
    if (!hasOs) return null;
    return mergeIsOsSeries(data, osData);
  }, [data, osData, hasOs]);

  const chartData = hasOs ? mergedData! : data;

  const monthlyTicks = useMemo(() => {
    if (chartData.length === 0) return [];
    const indices: number[] = [];
    let lastMonth = '';
    chartData.forEach((d, i) => {
      const month = d.date.slice(0, 7);
      if (month !== lastMonth) {
        indices.push(i);
        lastMonth = month;
      }
    });
    return indices;
  }, [chartData]);

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
          <LineChart data={chartData} margin={{ top: 8, right: 10, bottom: 20, left: 10 }}>
            <XAxis
              dataKey="date"
              ticks={monthlyTicks.map((i) => chartData[i]?.date).filter(Boolean)}
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
                if (hasOs) {
                  return [
                    Number.isFinite(n) ? fmt(n) : 'N/A',
                    name === 'isValue' ? 'IS' : 'OS',
                  ];
                }
                return [Number.isFinite(n) ? fmt(n) : 'N/A', title];
              }}
              labelFormatter={(_label, payload) => {
                const entry = payload?.[0]?.payload as { date: string } | undefined;
                return entry?.date ?? '';
              }}
            />
            {hasOs && (
              <Legend
                wrapperStyle={{ fontSize: 11 }}
                formatter={(value) => (value === 'isValue' ? 'IS' : 'OS')}
              />
            )}
            {hasOs ? (
              <>
                <Line
                  type="monotone"
                  dataKey="isValue"
                  stroke={color}
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="osValue"
                  stroke={osColor}
                  dot={false}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
              </>
            ) : (
              <Line
                type="monotone"
                dataKey="value"
                stroke={color}
                dot={false}
                strokeWidth={2}
                isAnimationActive={false}
              />
            )}
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
    (s) => s.config?.feeRate ?? 0.001
  );
  const capital = initialCapital ?? DEFAULT_CAPITAL;

  const fullRetSeries = useMemo(() => ({
    ...alpha.isRetSeries,
    ...alpha.osRetSeries,
  }), [alpha.isRetSeries, alpha.osRetSeries]);

  const fullTurnoverSeries = useMemo(() => ({
    ...alpha.isTurnoverSeries,
    ...alpha.osTurnoverSeries,
  }), [alpha.isTurnoverSeries, alpha.osTurnoverSeries]);

  const pnlBundle = useMemo(() => {
    return buildPnlBundleFromRet(fullRetSeries, fullTurnoverSeries, capital, transactionFeeRate);
  }, [fullRetSeries, fullTurnoverSeries, capital, transactionFeeRate]);

  const { isMddData, osMddData } = useMemo(() => {
    const isEntries: SingleSeriesPoint[] = [];
    const osEntries: SingleSeriesPoint[] = [];
    for (const p of pnlBundle.netEquityData) {
      if (alpha.osStartDate && p.date >= alpha.osStartDate) {
        osEntries.push(p);
      } else {
        isEntries.push(p);
      }
    }
    return {
      isMddData: buildMddDataFromEquity(isEntries),
      osMddData: buildMddDataFromEquity(osEntries),
    };
  }, [pnlBundle.netEquityData, alpha.osStartDate]);

  const turnoverData = useMemo(
    () => seriesToChartData(alpha.isTurnoverSeries),
    [alpha.isTurnoverSeries]
  );
  const osTurnoverData = useMemo(
    () => seriesToChartData(alpha.osTurnoverSeries),
    [alpha.osTurnoverSeries]
  );
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

  const osMinBetData = useMemo(
    () => seriesToChartData(alpha.osMinBetSeries),
    [alpha.osMinBetSeries]
  );
  const osMaxWeightData = useMemo(
    () => seriesToChartData(alpha.osMaxWeightSeries),
    [alpha.osMaxWeightSeries]
  );
  const osNonZeroData = useMemo(
    () => seriesToChartData(alpha.osNonzeroPositionSeries),
    [alpha.osNonzeroPositionSeries]
  );

  if (metric === 'pnl') {
    return <PnlDualChart pnlData={pnlBundle.pnlData} osStartDate={alpha.osStartDate} />;
  }

  if (metric === 'turnover') {
    return (
      <BigChart
        title="Turnover (%)"
        data={turnoverData}
        color="#0078ff"
        osData={osTurnoverData}
        osColor="#ff9800"
        formatValue={(v) => `${v.toFixed(1)}%`}
      />
    );
  }

  if (metric === 'mdd') {
    return (
      <BigChart
        title="MDD (%)"
        data={isMddData}
        color="#ff433d"
        osData={osMddData}
        osColor="#ff9800"
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
        osData={osMinBetData}
        osColor="#ff9800"
        formatValue={(v) => v.toFixed(4)}
      />
    );
  }

  if (metric === 'maxWeight') {
    return (
      <BigChart
        title="Max Weight"
        data={maxWeightData}
        color="#0078ff"
        osData={osMaxWeightData}
        osColor="#ff9800"
        formatValue={(v) => `${(v * 100).toFixed(2)}%`}
      />
    );
  }

  return (
    <BigChart
      title="Non-zero Position"
      data={nonZeroData}
      color="#0078ff"
      osData={osNonZeroData}
      osColor="#ff9800"
      formatValue={(v) => `${v.toFixed(0)}`}
    />
  );
}
