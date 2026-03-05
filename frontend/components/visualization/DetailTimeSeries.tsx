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
  fmtMonth,
  fmtDate,
  getDPR,
  TOOLTIP_STYLE,
} from '@/lib/uplotUtils';
import { useContainerSize, useUPlot } from '@/lib/uplotAdapters';
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

  let cumRet = 0;
  let cumGrossRet = 0;
  let grossKnown = true;
  const pnlData: PnlSeriesPoint[] = [];
  const netEquityData: SingleSeriesPoint[] = [];

  for (const [date, r] of retEntries) {
    const to = turnoverMap.get(date);
    const grossRet =
      grossKnown && typeof to === 'number'
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
const FMT_USD = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

const PnlDualChart = React.memo(function PnlDualChart({ pnlData, osStartDate }: { pnlData: PnlSeriesPoint[]; osStartDate?: string }) {
  const theme = useThemeStore(s => s.theme);
  const colors = useMemo(() => getThemeColors(theme), [theme]);

  const containerRef = useRef<HTMLDivElement>(null);
  const size = useContainerSize(containerRef);
  const tooltipRef = useRef<HTMLDivElement>(null);

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
    if (size.width === 0 || size.height === 0) return null;
    const [axisX, axisY] = buildUPlotAxes(colors);
    return {
      width: size.width,
      height: size.height,
      padding: [8, 10, 0, 0],
      cursor: {
        y: false,
        drag: { x: false, y: false },
      },
      legend: { show: false },
      scales: { x: { time: true }, y: { auto: true } },
      axes: [
        { ...axisX, values: (_u: uPlot, vals: number[]) => vals.map(fmtMonth), gap: 5, size: 40 },
        { ...axisY, show: false },
      ],
      series: [
        {},
        { stroke: '#4af6c3', width: 2, label: 'Net' },
        { stroke: '#ff9800', width: 1.5, label: 'Gross', dash: [4, 3] },
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
        setCursor: [(u: uPlot) => {
          const tt = tooltipRef.current;
          if (!tt) return;
          const { idx, left, top } = u.cursor;
          if (idx == null || left == null || left < 0) { tt.style.display = 'none'; return; }
          const netVal = u.data[1][idx] as number | null;
          const grossVal = u.data[2][idx] as number | null;
          if (netVal == null && grossVal == null) { tt.style.display = 'none'; return; }
          let html = `<div style="margin-bottom:2px;opacity:0.7">${fmtDate(u.data[0][idx])}</div>`;
          if (netVal != null) html += `<div><span style="color:#4af6c3">Net:</span> ${FMT_USD(netVal)}</div>`;
          if (grossVal != null) html += `<div><span style="color:#ff9800">Gross:</span> ${FMT_USD(grossVal)}</div>`;
          tt.innerHTML = html;
          tt.style.display = 'block';
          const dpr = getDPR();
          const bL = u.bbox.left / dpr;
          const bT = u.bbox.top / dpr;
          let x = left + bL + 15;
          const ttMaxW = 150;
          if (x + ttMaxW > size.width) {
            x = left + bL - ttMaxW - 15;
          }
          tt.style.left = `${x}px`;
          tt.style.top = `${(top ?? 0) + bT - 10}px`;
        }],
      },
    };
  }, [size.width, size.height, colors, osUnix]);

  useUPlot(containerRef, opts, uData, size);

  if (pnlData.length === 0) {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-2"><span className="text-xs text-foreground-muted">PnL ($)</span></div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col relative">
      <div className="px-3 py-2"><span className="text-xs text-foreground-muted">PnL ($)</span></div>
      <div ref={containerRef} className="flex-1 relative" />
      <div
        ref={tooltipRef}
        suppressHydrationWarning
        style={{ ...TOOLTIP_STYLE, backgroundColor: colors.backgroundElevated, border: `1px solid ${colors.chartGrid}`, color: colors.chartText }}
      />
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
  const tooltipRef = useRef<HTMLDivElement>(null);

  const uData = useMemo<uPlot.AlignedData>(() => {
    if (hasOs && mergedData) {
      return toMultiSeriesData(mergedData as unknown as Record<string, unknown>[], 'date', ['isValue', 'osValue']);
    }
    return toAlignedData(data.map(d => ({ date: d.date, value: d.value })));
  }, [data, mergedData, hasOs]);

  const opts = useMemo<uPlot.Options | null>(() => {
    if (size.width === 0 || size.height === 0) return null;
    const [axisX, axisY] = buildUPlotAxes(colors);
    const seriesDefs: uPlot.Series[] = hasOs
      ? [{}, { stroke: color, width: 2, label: 'IS' }, { stroke: osColor ?? '#ff9800', width: 2, label: 'OS' }]
      : [{}, { stroke: color, width: 2, label: title }];

    return {
      width: size.width,
      height: size.height,
      padding: [8, 10, 0, 0],
      cursor: { y: false, drag: { x: false, y: false } },
      legend: { show: false },
      scales: { x: { time: true }, y: { auto: true } },
      axes: [
        { ...axisX, values: (_u: uPlot, vals: number[]) => vals.map(fmtMonth), gap: 5, size: 40 },
        { ...axisY, show: false },
      ],
      series: seriesDefs,
      hooks: {
        setCursor: [(u: uPlot) => {
          const tt = tooltipRef.current;
          if (!tt) return;
          const { idx, left, top } = u.cursor;
          if (idx == null || left == null || left < 0) { tt.style.display = 'none'; return; }
          let html = `<div style="margin-bottom:2px;opacity:0.7">${fmtDate(u.data[0][idx])}</div>`;
          if (hasOs) {
            const isV = u.data[1][idx] as number | null;
            const osV = u.data[2][idx] as number | null;
            if (isV == null && osV == null) { tt.style.display = 'none'; return; }
            if (isV != null) html += `<div><span style="color:${color}">IS:</span> ${fmt(isV)}</div>`;
            if (osV != null) html += `<div><span style="color:${osColor ?? '#ff9800'}">OS:</span> ${fmt(osV)}</div>`;
          } else {
            const v = u.data[1][idx] as number | null;
            if (v == null) { tt.style.display = 'none'; return; }
            html += `<div>${fmt(v)}</div>`;
          }
          tt.innerHTML = html;
          tt.style.display = 'block';
          const dpr = getDPR();
          const bL = u.bbox.left / dpr;
          const bT = u.bbox.top / dpr;
          let x = left + bL + 15;
          const ttMaxW = 150;
          if (x + ttMaxW > size.width) {
            x = left + bL - ttMaxW - 15;
          }
          tt.style.left = `${x}px`;
          tt.style.top = `${(top ?? 0) + bT - 10}px`;
        }],
      },
    };
  }, [size.width, size.height, colors, color, osColor, hasOs, title, fmt]);

  useUPlot(containerRef, opts, uData, size);

  if (data.length === 0) {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-2"><span className="text-xs text-foreground-muted">{title}</span></div>
        <div className="flex-1 bg-chart-background" />
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col relative">
      <div className="px-3 py-2"><span className="text-xs text-foreground-muted">{title}</span></div>
      <div ref={containerRef} className="flex-1 relative" />
      <div
        ref={tooltipRef}
        suppressHydrationWarning
        style={{ ...TOOLTIP_STYLE, backgroundColor: colors.backgroundElevated, border: `1px solid ${colors.chartGrid}`, color: colors.chartText }}
      />
    </div>
  );
});

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
    const fullMdd = buildMddDataFromEquity(pnlBundle.netEquityData);
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
        formatValue={FMT_TURNOVER}
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
        formatValue={FMT_MDD}
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
        formatValue={FMT_MINBET}
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
        formatValue={FMT_MAXWGT}
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
      formatValue={FMT_NONZERO}
    />
  );
}
