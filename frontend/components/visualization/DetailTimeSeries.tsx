'use client';

import { useMemo } from 'react';
import { LineChart, Line, ResponsiveContainer, XAxis, YAxis, Tooltip } from 'recharts';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import type { BacktestAlphaResult } from '@/types';

export type DetailMetricKey = 'pnl' | 'mdd' | 'minBet' | 'maxWeight' | 'nonZero';

interface ChartData {
  date: string;
  value: number;
}

function seriesToChartData(series: Record<string, number> | null | undefined): ChartData[] {
  if (!series) return [];
  return Object.entries(series)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, value]) => ({ date, value }));
}

function BigChart({ title, data, color, formatValue }: {
  title: string;
  data: ChartData[];
  color: string;
  formatValue?: (v: number) => string;
}) {
  const fmt = formatValue ?? ((v: number) => v.toFixed(4));
  const { theme } = useThemeStore();
  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => { void theme; return getThemeColors(); }, [theme]);

  // Build monthly tick indices for XAxis
  const monthlyTicks = useMemo(() => {
    if (data.length === 0) return [];
    const indices: number[] = [];
    let lastMonth = '';
    data.forEach((d, i) => {
      const month = d.date.slice(0, 7); // "YYYY-MM"
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
        <div className="flex-1 rounded-lg bg-chart-background" />
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
                backgroundColor: colors.chartBackground,
                border: `1px solid ${colors.chartGrid}`,
                fontSize: 12,
                color: colors.chartText,
              }}
              labelStyle={{ color: colors.chartText, fontSize: 11 }}
              formatter={(value) => [fmt(Number(value) || 0), title]}
              labelFormatter={(_label, payload) => {
                const entry = payload?.[0]?.payload;
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

export default function DetailTimeSeries({
  alpha,
  metric,
}: {
  alpha: BacktestAlphaResult;
  metric: DetailMetricKey;
}) {
  const pnlData = useMemo(() => {
    // Prefer pre-computed equity series from backend
    const eq = alpha.isEquitySeries;
    if (eq) {
      return Object.entries(eq)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, value]) => ({ date, value }));
    }
    // Fallback: compute from return series
    const ret = alpha.isRetSeries;
    if (!ret) return [];
    const entries = Object.entries(ret).sort(([a], [b]) => a.localeCompare(b));
    let equity = 1;
    return entries.map(([date, r]) => {
      equity *= (1 + r);
      return { date, value: equity };
    });
  }, [alpha.isEquitySeries, alpha.isRetSeries]);

  const mddData = useMemo(() => {
    if (pnlData.length === 0) return [];
    let peak = -Infinity;
    return pnlData.map(({ date, value }) => {
      if (value > peak) peak = value;
      const dd = (value / peak - 1) * 100;
      return { date, value: dd };
    });
  }, [pnlData]);

  const minBetData = useMemo(() => seriesToChartData(alpha.isMinBetSeries), [alpha.isMinBetSeries]);
  const maxWeightData = useMemo(() => seriesToChartData(alpha.isMaxWeightSeries), [alpha.isMaxWeightSeries]);

  if (metric === 'nonZero') {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-2">
          <span className="text-xs text-foreground-muted">Non-zero Position</span>
        </div>
        <div className="flex-1 rounded-lg bg-chart-background" />
      </div>
    );
  }

  if (metric === 'pnl') {
    return (
      <BigChart
        title="PnL ($)"
        data={pnlData}
        color="#22c55e"
        formatValue={(v) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
      />
    );
  }

  if (metric === 'mdd') {
    return (
      <BigChart
        title="MDD (%)"
        data={mddData}
        color="#ef4444"
        formatValue={(v) => `${v.toFixed(2)}%`}
      />
    );
  }

  if (metric === 'minBet') {
    return (
      <BigChart
        title="Min Bet"
        data={minBetData}
        color="#a855f7"
        formatValue={(v) => v.toFixed(4)}
      />
    );
  }

  return (
    <BigChart
      title="Max Weight"
      data={maxWeightData}
      color="#f59e0b"
      formatValue={(v) => (v * 100).toFixed(2) + '%'}
    />
  );
}
