'use client';

import React, { useMemo, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { useThemeStore } from '@/store/themeStore';
import { useAlphaStore } from '@/store/alphaStore';
import { getThemeColors } from '@/lib/themeColors';
import {
  dateStrToUnix,
  toMultiSeriesData,
  toAlignedData,
  buildUPlotAxes,
} from '@/lib/uplotUtils';
import { chartInteraction } from '@/lib/chartInteraction';
import { useContainerSize, useUPlot } from '@/lib/uplotAdapters';
import type { BacktestAlphaResult } from '@/types';
import { REPORT_BOOK_DOLLARS, MILLION_DOLLARS } from '@/lib/reportUnits';

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

interface IsOsSeriesPoint {
  date: string;
  isValue?: number;
  osValue?: number;
}

// Use the same period colors and line weight across every detail metric.
const PERIOD_IS_COLOR = '#df8e13';
const PERIOD_OS_COLOR = '#258249';

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
  // Engine gross (pre-fee) returns; preferred over net + turnover * fee.
  grossRetSeries?: Record<string, number | null> | null,
): { pnlData: PnlSeriesPoint[]; grossEquityData: SingleSeriesPoint[] } {
  if (!retSeries) return { pnlData: [], grossEquityData: [] };

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

  let cumRet = 0;
  let cumGrossRet = 0;
  let grossKnown = true;
  const pnlData: PnlSeriesPoint[] = [];
  const grossEquityData: SingleSeriesPoint[] = [];

  for (const [date, r] of retEntries) {
    const to = turnoverMap.get(date);
    const engineGross = grossRetSeries ? grossRetSeries[date] : undefined;
    const grossRet = !grossKnown
      ? Number.NaN
      : grossRetSeries
        ? (typeof engineGross === 'number' ? engineGross : Number.NaN)
        : typeof to === 'number'
          ? r + (to / 100) * transactionFeeRate
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
    // Drawdown follows gross PnL, matching the engine's gross metrics.
    if (grossKnown) grossEquityData.push({ date, value: capital + cumGrossRet * capital });
  }

  return { pnlData, grossEquityData };
}

export function buildMddDataFromEquity(
  equityData: SingleSeriesPoint[],
  initialCapital: number,
): SingleSeriesPoint[] {
  if (equityData.length === 0) return [];
  if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
    throw new Error('Drawdown requires positive initial capital');
  }
  let peak = initialCapital;
  return equityData.map(({ date, value }) => {
    if (value > peak) peak = value;
    // Fixed-book additive PnL: percentage points of initial capital.
    const dd = ((value - peak) / initialCapital) * 100;
    return { date, value: dd };
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

// ── Stable format functions (hoisted to avoid useMemo invalidation) ──
const FMT_TURNOVER = (v: number) => `${v.toFixed(1)}%`;
const FMT_MDD = (v: number) => `${v.toFixed(2)}%`;
const FMT_MINBET = (v: number) => v.toFixed(4);
const FMT_MAXWGT = (v: number) => `${v.toFixed(2)}%`;
const FMT_NONZERO = (v: number) => `${v.toFixed(0)}`;
const FMT_USD = (v: number) => `$${(v / MILLION_DOLLARS).toFixed(2)}M`;

const PnlDualChart = React.memo(function PnlDualChart({ pnlData, osStartDate }: { pnlData: PnlSeriesPoint[]; osStartDate?: string }) {
  const theme = useThemeStore(s => s.theme);
  const colors = useMemo(() => getThemeColors(theme), [theme]);

  const containerRef = useRef<HTMLDivElement>(null);
  const size = useContainerSize(containerRef);

  const uData = useMemo<uPlot.AlignedData>(() => {
    const len = pnlData.length;
    const ts: number[] = new Array(len);
    const netVals: (number | null)[] = new Array(len);
    const grossVals: (number | null)[] = new Array(len);
    for (let i = 0; i < len; i++) {
      ts[i] = dateStrToUnix(pnlData[i].date);
      netVals[i] = pnlData[i].net;
      grossVals[i] = Number.isFinite(pnlData[i].gross) ? pnlData[i].gross : null;
    }
    return [ts, netVals, grossVals];
  }, [pnlData]);

  const osUnix = useMemo(() => osStartDate ? dateStrToUnix(osStartDate) : null, [osStartDate]);

  const opts = useMemo<uPlot.Options | null>(() => {
    const [axisX, axisY] = buildUPlotAxes(colors);
    // Both cumulative curves stay continuous; only their color changes at OS.
    // Gross/Net use solid/dashed strokes, IS/OS use amber/green.
    const periodStroke = (u: uPlot) => {
      if (osUnix === null) return PERIOD_IS_COLOR;
      const split = u.valToPos(osUnix, 'x', true);
      const { left, width } = u.bbox;
      if (split <= left) return PERIOD_OS_COLOR;
      if (split >= left + width || width <= 0) return PERIOD_IS_COLOR;
      const gradient = u.ctx.createLinearGradient(left, 0, left + width, 0);
      const fraction = (split - left) / width;
      gradient.addColorStop(0, PERIOD_IS_COLOR);
      gradient.addColorStop(fraction, PERIOD_IS_COLOR);
      gradient.addColorStop(fraction, PERIOD_OS_COLOR);
      gradient.addColorStop(1, PERIOD_OS_COLOR);
      return gradient;
    };
    return {
      width: 1,
      height: 1,
      padding: [8, 10, 0, 0],
      cursor: {
        y: false,
        drag: { x: false, y: false },
      },
      legend: { show: false },
      plugins: [chartInteraction(FMT_USD, false)],
      scales: { x: { time: true }, y: { auto: true } },
      axes: [
        { ...axisX, gap: 5, size: 40 },
        { ...axisY, show: true, size: 70, values: (_u, values) => values.map(v => (v / MILLION_DOLLARS).toLocaleString(undefined, { maximumFractionDigits: 2 })) },
      ],
      series: [
        {},
        { stroke: periodStroke, width: 2, label: 'Net', dash: [4, 3], points: { show: false } },
        { stroke: periodStroke, width: 2, label: 'Gross', points: { show: false } },
      ],
      hooks: {
        draw: [
          (u: uPlot) => {
            if (osUnix == null) return;
            const cx = u.valToPos(osUnix, 'x', true);
            if (cx < u.bbox.left || cx > u.bbox.left + u.bbox.width) return;
            const ctx = u.ctx;
            ctx.save();
            ctx.strokeStyle = colors.chartGrid;
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(cx, u.bbox.top);
            ctx.lineTo(cx, u.bbox.top + u.bbox.height);
            ctx.stroke();
            // label
            ctx.setLineDash([]);
            ctx.fillStyle = colors.chartText;
            ctx.font = '10px "JetBrains Mono", monospace';
            ctx.textAlign = 'left';
            ctx.fillText('OS', cx + 4, u.bbox.top + 12);
            ctx.restore();
          },
        ],
      },
    };
  }, [colors, osUnix]);

  useUPlot(containerRef, opts, uData, size, pnlData);

  if (pnlData.length === 0) {
    return (
      <div className="h-full min-h-0 min-w-0 flex flex-col">
        <div className="flex shrink-0 items-center gap-3 px-2 py-1 overflow-x-auto whitespace-nowrap [&>span]:shrink-0 text-[10px] text-foreground-muted">
        <span>PnL ($M)</span>
        <span className="text-foreground-secondary">━ Gross</span>
        <span className="text-foreground-secondary">┄ Net</span>
        <span style={{ color: PERIOD_IS_COLOR }}>━ IS</span>
        {osUnix !== null ? <span style={{ color: PERIOD_OS_COLOR }}>━ OS</span> : null}
      </div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 min-w-0 flex flex-col relative">
      <div className="flex shrink-0 items-center gap-3 px-2 py-1 overflow-x-auto whitespace-nowrap [&>span]:shrink-0 text-[10px] text-foreground-muted">
        <span>PnL ($M)</span>
        <span className="text-foreground-secondary">━ Gross</span>
        <span className="text-foreground-secondary">┄ Net</span>
        <span style={{ color: PERIOD_IS_COLOR }}>━ IS</span>
        {osUnix !== null ? <span style={{ color: PERIOD_OS_COLOR }}>━ OS</span> : null}
      </div>
      <div ref={containerRef} className="flex-1 min-h-0 min-w-0 relative" />
    </div>
  );
});

const BigChart = React.memo(function BigChart({
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
  const fmt = formatValue ?? FMT_MINBET;
  const theme = useThemeStore(s => s.theme);
  const colors = useMemo(() => getThemeColors(theme), [theme]);

  const hasOs = osData && osData.length > 0;
  const mergedData = useMemo(() => {
    if (!hasOs) return null;
    return mergeIsOsSeries(data, osData);
  }, [data, osData, hasOs]);

  const containerRef = useRef<HTMLDivElement>(null);
  const size = useContainerSize(containerRef);

  const uData = useMemo<uPlot.AlignedData>(() => {
    if (hasOs && mergedData) {
      return toMultiSeriesData(mergedData as unknown as Record<string, unknown>[], 'date', ['isValue', 'osValue']);
    }
    return toAlignedData(data.map(d => ({ date: d.date, value: d.value })));
  }, [data, mergedData, hasOs]);

  const opts = useMemo<uPlot.Options | null>(() => {
    const [axisX, axisY] = buildUPlotAxes(colors);
    const seriesDefs: uPlot.Series[] = hasOs
      ? [{}, { stroke: color, width: 2, label: 'IS' }, { stroke: osColor ?? PERIOD_OS_COLOR, width: 2, label: 'OS' }]
      : [{}, { stroke: color, width: 2, label: title }];

    return {
      width: 1,
      height: 1,
      padding: [8, 10, 0, 0],
      cursor: { y: false, drag: { x: false, y: false } },
      legend: { show: false },
      plugins: [chartInteraction(fmt, false)],
      scales: { x: { time: true }, y: { auto: true } },
      axes: [
        { ...axisX, gap: 5, size: 40 },
        { ...axisY, show: true, size: 70 },
      ],
      series: seriesDefs,
      hooks: {
      },
    };
  }, [colors, color, osColor, hasOs, title, fmt]);

  useUPlot(containerRef, opts, uData, size, data);

  if (data.length === 0) {
    return (
      <div className="h-full min-h-0 min-w-0 flex flex-col">
        <div className="flex shrink-0 items-center gap-3 px-2 py-1 overflow-x-auto whitespace-nowrap [&>span]:shrink-0 text-[10px] text-foreground-muted">
        <span>{title}</span>
        {hasOs ? <><span style={{ color }}>━ IS</span><span style={{ color: osColor }}>━ OS</span></> : null}
      </div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 min-w-0 flex flex-col relative">
      <div className="flex shrink-0 items-center gap-3 px-2 py-1 overflow-x-auto whitespace-nowrap [&>span]:shrink-0 text-[10px] text-foreground-muted">
        <span>{title}</span>
        {hasOs ? <><span style={{ color }}>━ IS</span><span style={{ color: osColor }}>━ OS</span></> : null}
      </div>
      <div ref={containerRef} className="flex-1 min-h-0 min-w-0 relative" />
    </div>
  );
});

export default function DetailTimeSeries({
  alpha,
  metric,
}: {
  alpha: BacktestAlphaResult;
  metric: DetailMetricKey;
}) {
  const transactionFeeRate = useAlphaStore(
    (s) => s.config?.feeRate ?? 0.001
  );
  const capital = REPORT_BOOK_DOLLARS;

  const fullRetSeries = useMemo(() => ({
    ...alpha.isRetSeries,
    ...alpha.osRetSeries,
  }), [alpha.isRetSeries, alpha.osRetSeries]);

  const fullTurnoverSeries = useMemo(() => ({
    ...alpha.isTurnoverSeries,
    ...alpha.osTurnoverSeries,
  }), [alpha.isTurnoverSeries, alpha.osTurnoverSeries]);

  const fullGrossRetSeries = useMemo(() => (
    alpha.isGrossRetSeries || alpha.osGrossRetSeries
      ? { ...alpha.isGrossRetSeries, ...alpha.osGrossRetSeries }
      : null
  ), [alpha.isGrossRetSeries, alpha.osGrossRetSeries]);

  const pnlBundle = useMemo(() => {
    return buildPnlBundleFromRet(
      fullRetSeries, fullTurnoverSeries, capital, transactionFeeRate, fullGrossRetSeries);
  }, [fullRetSeries, fullTurnoverSeries, capital, transactionFeeRate, fullGrossRetSeries]);

  const { isMddData, osMddData } = useMemo(() => {
    const fullMdd = buildMddDataFromEquity(pnlBundle.grossEquityData, capital);
    const isEntries: SingleSeriesPoint[] = [];
    const osEntries: SingleSeriesPoint[] = [];
    for (const p of fullMdd) {
      if (alpha.osStartDate && p.date >= alpha.osStartDate) {
        osEntries.push(p);
      } else {
        isEntries.push(p);
      }
    }
    return { isMddData: isEntries, osMddData: osEntries };
  }, [pnlBundle.grossEquityData, alpha.osStartDate, capital]);

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
        color={PERIOD_IS_COLOR}
        osData={osTurnoverData}
        osColor={PERIOD_OS_COLOR}
        formatValue={FMT_TURNOVER}
      />
    );
  }

  if (metric === 'mdd') {
    return (
      <BigChart
        title="MDD (%)"
        data={isMddData}
        color={PERIOD_IS_COLOR}
        osData={osMddData}
        osColor={PERIOD_OS_COLOR}
        formatValue={FMT_MDD}
      />
    );
  }

  if (metric === 'minBet') {
    return (
      <BigChart
        title="Min Bet"
        data={minBetData}
        color={PERIOD_IS_COLOR}
        osData={osMinBetData}
        osColor={PERIOD_OS_COLOR}
        formatValue={FMT_MINBET}
      />
    );
  }

  if (metric === 'maxWeight') {
    return (
      <BigChart
        title="Max Weight"
        data={maxWeightData}
        color={PERIOD_IS_COLOR}
        osData={osMaxWeightData}
        osColor={PERIOD_OS_COLOR}
        formatValue={FMT_MAXWGT}
      />
    );
  }

  return (
    <BigChart
      title="Non-zero Position"
      data={nonZeroData}
      color={PERIOD_IS_COLOR}
      osData={osNonZeroData}
      osColor={PERIOD_OS_COLOR}
      formatValue={FMT_NONZERO}
    />
  );
}
