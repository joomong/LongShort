'use client';

import React, { useMemo, useCallback, useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { useAlphaStore } from '@/store/alphaStore';
import { selectSignalSource } from '@/lib/signalSelection';
import { useShallow } from 'zustand/react/shallow';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { getNextColor } from '@/lib/snapshotColors';
import {
  toMultiSeriesData,
  buildUPlotAxes,
  fmtMonth,
  fmtDate,
  getDPR,
  TOOLTIP_STYLE,
} from '@/lib/uplotUtils';
import { useContainerSize, useUPlot } from '@/lib/uplotAdapters';

interface ChartDataPoint {
  date: string;
  value: number | null;
}

// ── Stable constants ──────────────────────────────────────────────────
const EMPTY_DATA: ChartDataPoint[] = [];
// content-visibility: auto skips layout/paint for off-screen table rows
const ROW_CV_STYLE: React.CSSProperties = { contentVisibility: 'auto', containIntrinsicBlockSize: '18px' };

function formatSignalValue(n: number): string {
  const abs = Math.abs(n);
  return abs >= 1000 ? n.toFixed(1) : abs >= 1 ? n.toFixed(3) : n.toFixed(6);
}

// ── Extracted data table (isolated from chart re-renders) ──
const SignalDataTable = React.memo(function SignalDataTable({
  data,
  selectedSymbol,
}: {
  data: ChartDataPoint[];
  selectedSymbol: string | null;
}) {
  const tableScrollRef = useRef<HTMLDivElement>(null);

  const firstDataIndex = useMemo(() => {
    for (let i = 0; i < data.length; i++) {
      if (data[i].value !== null) return i;
    }
    return 0;
  }, [data]);

  useEffect(() => {
    if (firstDataIndex > 0 && tableScrollRef.current) {
      const rowHeight = 18;
      tableScrollRef.current.scrollTop = Math.max(0, (firstDataIndex - 3) * rowHeight);
    }
  }, [firstDataIndex, selectedSymbol]);

  return (
    <div className="w-[190px] shrink-0 border-r border-border overflow-hidden flex flex-col">
      <div className="px-2 py-1 border-b border-border bg-background-secondary/50">
        <span className="text-[10px] text-foreground-muted font-semibold uppercase tracking-wide">
          {selectedSymbol ?? 'signal'} data
        </span>
      </div>
      <div ref={tableScrollRef} className="flex-1 overflow-y-auto">
        <table aria-label="Signal data" className="w-full text-[11px] border-collapse font-mono">
          <thead className="sticky top-0 bg-background-secondary">
            <tr className="border-b border-border-strong">
              <th className="text-left px-2 py-0.5 text-foreground-secondary font-medium">dates</th>
              <th className="text-right px-2 py-0.5 text-foreground-secondary font-medium">signal</th>
            </tr>
          </thead>
          <tbody>
            {data.map(row => {
              const v = row.value;
              const hasValue = v !== null && v !== undefined;
              return (
                <tr key={row.date} className="border-b border-border/20 hover:bg-row-hover" style={ROW_CV_STYLE}>
                  <td className="px-2 py-px tabular-nums text-foreground-muted whitespace-nowrap">{row.date}</td>
                  <td className={`px-2 py-px tabular-nums text-right whitespace-nowrap ${hasValue ? 'text-data-color' : 'text-foreground-muted'}`}>
                    {hasValue ? v.toFixed(4) : 'NaN'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
});

export default function SignalChart() {
  // Granular selectors
  const heatmap = useAlphaStore(s => selectSignalSource(s)?.heatmap ?? null);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const pinnedSnapshots = useAlphaStore(useShallow(s => s.pinnedSnapshots));
  const addSnapshot = useAlphaStore(s => s.addSnapshot);
  const theme = useThemeStore(s => s.theme);
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  // ── Measure container size with ResizeObserver ──
  const size = useContainerSize(containerRef);

  // Re-read CSS custom properties when theme toggles
  const colors = useMemo(() => getThemeColors(theme), [theme]);

  // Build symbol index Map for O(1) lookup
  const symbolMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.symbols.map((s, i) => [s, i]));
  }, [heatmap]);

  const data = useMemo<ChartDataPoint[]>(() => {
    if (!heatmap || !selectedSymbol) return EMPTY_DATA;
    const symbolIdx = symbolMap.get(selectedSymbol);
    if (symbolIdx === undefined) return EMPTY_DATA;
    return heatmap.timestamps.map((ts, timeIdx) => {
      const v = (heatmap.values as unknown as Array<Array<number | null | undefined>>)[timeIdx]?.[symbolIdx];
      return {
        date: ts,
        value: typeof v === 'number' && Number.isFinite(v) ? v : null,
      };
    });
  }, [heatmap, selectedSymbol, symbolMap]);

  // Merge snapshot series into chart data
  const mergedData = useMemo(() => {
    if (pinnedSnapshots.length === 0) return data;
    const dateMap = new Map<string, Record<string, number | null>>();
    for (const pt of data) {
      dateMap.set(pt.date, { value: pt.value });
    }
    for (const snap of pinnedSnapshots) {
      for (const [date, val] of Object.entries(snap.retSeries)) {
        const existing = dateMap.get(date);
        if (existing) {
          existing[`snap_${snap.id}`] = val;
        } else {
          dateMap.set(date, { value: null, [`snap_${snap.id}`]: val });
        }
      }
    }
    const allDates = Array.from(dateMap.keys()).toSorted();
    return allDates.map((date) => ({
      date,
      ...dateMap.get(date)!,
    }));
  }, [data, pinnedSnapshots]);

  // Convert to uPlot AlignedData
  const seriesKeys = useMemo(
    () => ['value', ...pinnedSnapshots.map(s => `snap_${s.id}`)],
    [pinnedSnapshots]
  );
  const uData = useMemo<uPlot.AlignedData>(
    () => toMultiSeriesData(mergedData as Record<string, unknown>[], 'date', seriesKeys),
    [mergedData, seriesKeys]
  );

  // ── Drag state (refs only — no React re-renders during interaction) ──
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef<{
    x: number; y: number;
    xMin: number; xMax: number;
    yMin: number; yMax: number;
  } | null>(null);

  // ── uPlot options (memoized to avoid unnecessary recreation) ──
  const opts = useMemo<uPlot.Options | null>(() => {
    if (size.width === 0 || size.height === 0) return null;
    const [axisX, axisY] = buildUPlotAxes(colors);

    const seriesDefs: uPlot.Series[] = [
      {},
      {
        stroke: colors.chartLine,
        width: 2,
        label: 'Signal',
        points: { show: true, size: 4, fill: colors.chartLine, stroke: colors.chartLine },
      },
      ...pinnedSnapshots.map(s => ({
        stroke: s.color,
        width: 1.5,
        label: s.name,
        spanGaps: true,
        points: { show: false },
      } as uPlot.Series)),
    ];

    return {
      width: size.width,
      height: size.height,
      padding: [10, 30, 0, 0],
      cursor: {
        drag: { x: false, y: false },
      },
      legend: { show: false },
      scales: {
        x: { time: true },
        y: { auto: true },
      },
      axes: [
        {
          ...axisX,
          values: (_u: uPlot, vals: number[]) => vals.map(fmtMonth),
          gap: 40,
        },
        {
          ...axisY,
          values: (_u: uPlot, vals: number[]) => vals.map(v => v.toFixed(3)),
          size: 60,
          grid: { ...axisY.grid, dash: [3, 3] },
        },
      ],
      series: seriesDefs,
      hooks: {
        setCursor: [(u: uPlot) => {
          const tt = tooltipRef.current;
          if (!tt) return;
          const { idx, left, top } = u.cursor;
          if (idx == null || left == null || left < 0) { tt.style.display = 'none'; return; }

          let html = `<div style="margin-bottom:2px;opacity:0.7">${fmtDate(u.data[0][idx])}</div>`;
          let hasAny = false;
          for (let i = 1; i < u.series.length; i++) {
            const v = u.data[i]?.[idx];
            if (v == null) continue;
            hasAny = true;
            const s = u.series[i];
            const label = s.label || 'Value';
            html += `<div><span style="color:${s.stroke}">${label}:</span> ${formatSignalValue(v as number)}</div>`;
          }
          if (!hasAny) { tt.style.display = 'none'; return; }

          tt.innerHTML = html;
          tt.style.display = 'block';
          const dpr = getDPR();
          const bL = u.bbox.left / dpr;
          const bT = u.bbox.top / dpr;
          let x = left + bL + 15;
          // Use fixed max-width (150px) instead of reading tt.offsetWidth to avoid layout thrash
          const ttMaxW = 150;
          if (x + ttMaxW > size.width) {
            x = left + bL - ttMaxW - 15;
          }
          tt.style.left = `${x}px`;
          tt.style.top = `${(top ?? 0) + bT - 10}px`;
        }],
      },
    };
  }, [size.width, size.height, colors, pinnedSnapshots]);

  // ── uPlot lifecycle (shared hook) ──
  const uplotRef = useUPlot(containerRef, opts, uData, size);

  // Reset viewport when data changes (symbol changed)
  useEffect(() => {
    const u = uplotRef.current;
    if (!u || uData[0].length === 0) return;
    const ts = uData[0];
    if (ts.length > 365) {
      u.setScale('x', { min: ts[ts.length - 365], max: ts[ts.length - 1] });
    } else {
      u.setScale('x', { min: ts[0], max: ts[ts.length - 1] });
    }
  }, [uData, uplotRef]);

  // ── Wheel handler: zoom + horizontal pan via setScale ──
  // Attached as a native listener with { passive: false } so preventDefault works
  const handleWheel = useCallback((e: WheelEvent) => {
    e.preventDefault();
    const u = uplotRef.current;
    if (!u || !u.data[0] || u.data[0].length === 0) return;

    const xMin = u.scales.x.min!;
    const xMax = u.scales.x.max!;
    const xRange = xMax - xMin;
    const dataMin = u.data[0][0];
    const dataMax = u.data[0][u.data[0].length - 1];

    const isHorizontalPan = Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey;
    if (isHorizontalPan) {
      const plotWidth = u.bbox.width / getDPR();
      const shift = (e.deltaX / plotWidth) * xRange;
      let newMin = xMin + shift;
      let newMax = xMax + shift;
      if (newMin < dataMin) { newMax += (dataMin - newMin); newMin = dataMin; }
      if (newMax > dataMax) { newMin -= (newMax - dataMax); newMax = dataMax; }
      u.setScale('x', { min: newMin, max: newMax });
      return;
    }

    // Zoom — cursor-anchored: the data point under the mouse stays fixed on screen
    const zoomFactor = e.deltaY > 0 ? 1.1 : 0.9;
    const overRect = u.over.getBoundingClientRect();
    const mouseOffsetX = e.clientX - overRect.left;
    const t = Math.max(0, Math.min(1, mouseOffsetX / overRect.width));
    const cursorVal = xMin + t * xRange;

    const newRange = xRange * zoomFactor;
    const spacing = u.data[0].length > 1 ? u.data[0][1] - u.data[0][0] : 86400;
    const minRange = spacing * 5;
    const maxRange = dataMax - dataMin;
    const clamped = Math.max(minRange, Math.min(maxRange, newRange));

    let newMin = cursorVal - t * clamped;
    let newMax = cursorVal + (1 - t) * clamped;
    if (newMin < dataMin) { newMin = dataMin; newMax = newMin + clamped; }
    if (newMax > dataMax) { newMax = dataMax; newMin = newMax - clamped; }

    u.setScale('x', { min: newMin, max: newMax });
  }, [uplotRef]);

  // Attach native wheel listener with { passive: false } so preventDefault is honoured
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, [handleWheel]);

  // ── Drag pan: reads/writes uPlot scales directly, zero React state ──
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    const u = uplotRef.current;
    if (!u || e.button !== 0) return;
    e.preventDefault();
    isDraggingRef.current = true;
    if (containerRef.current) containerRef.current.style.cursor = 'grabbing';
    dragStartRef.current = {
      x: e.clientX,
      y: e.clientY,
      xMin: u.scales.x.min!,
      xMax: u.scales.x.max!,
      yMin: u.scales.y.min!,
      yMax: u.scales.y.max!,
    };
  }, [uplotRef]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    const ds = dragStartRef.current;
    const u = uplotRef.current;
    if (!isDraggingRef.current || !ds || !u) return;

    const dpr = getDPR();
    const plotWidth = u.bbox.width / dpr;
    const plotHeight = u.bbox.height / dpr;
    const deltaX = e.clientX - ds.x;
    const deltaY = e.clientY - ds.y;

    const xShift = (deltaX / plotWidth) * (ds.xMax - ds.xMin) * -1;
    const yShift = (deltaY / plotHeight) * (ds.yMax - ds.yMin);

    u.batch(() => {
      u.setScale('x', { min: ds.xMin + xShift, max: ds.xMax + xShift });
      u.setScale('y', { min: ds.yMin + yShift, max: ds.yMax + yShift });
    });
  }, [uplotRef]);

  const handleMouseUp = useCallback(() => {
    isDraggingRef.current = false;
    dragStartRef.current = null;
    if (containerRef.current) containerRef.current.style.cursor = 'grab';
  }, []);

  const handleDoubleClick = useCallback(() => {
    const u = uplotRef.current;
    if (!u || !u.data[0] || u.data[0].length === 0) return;
    // Reset X to full range
    u.setScale('x', { min: u.data[0][0], max: u.data[0][u.data[0].length - 1] });
    // Reset Y by computing data extent
    let yMin = Infinity, yMax = -Infinity;
    for (let s = 1; s < u.data.length; s++) {
      const series = u.data[s];
      if (!series) continue;
      for (let i = 0; i < series.length; i++) {
        const v = series[i];
        if (v != null && Number.isFinite(v)) {
          if (v < yMin) yMin = v;
          if (v > yMax) yMax = v;
        }
      }
    }
    if (yMin < yMax) {
      const pad = (yMax - yMin) * 0.05;
      u.setScale('y', { min: yMin - pad, max: yMax + pad });
    }
  }, [uplotRef]);

  // Global mouseup listener (cleanup on unmount)
  useEffect(() => {
    const handler = () => {
      if (isDraggingRef.current) {
        isDraggingRef.current = false;
        dragStartRef.current = null;
        if (containerRef.current) containerRef.current.style.cursor = 'grab';
      }
    };
    window.addEventListener('mouseup', handler);
    return () => window.removeEventListener('mouseup', handler);
  }, []);

  const handlePin = useCallback(() => {
    if (data.length === 0) return;
    const retSeries: Record<string, number> = {};
    for (const pt of data) {
      if (pt.value !== null) retSeries[pt.date] = pt.value;
    }
    const usedColors = pinnedSnapshots.map((s) => s.color);
    const color = getNextColor(usedColors);
    const name = selectedSymbol ?? `Snapshot ${pinnedSnapshots.length + 1}`;
    addSnapshot({
      id: Date.now().toString(36),
      name,
      color,
      retSeries,
    });
  }, [data, pinnedSnapshots, selectedSymbol, addSnapshot]);

  const hasData = data.length > 0 && data.some(d => d.value !== null);

  return (
    <div className="flex-1 flex flex-row bg-background-tertiary min-h-0 panel-inset">
      {/* Left: STAR-style data table (memo-isolated from chart re-renders) */}
      <SignalDataTable data={data} selectedSymbol={selectedSymbol} />

      {/* Right: uPlot chart */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
        <div
          ref={containerRef}
          className="relative flex-1 w-full min-h-0 p-2 select-none overflow-hidden"
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onDoubleClick={handleDoubleClick}
          style={{ cursor: hasData ? 'grab' : 'default' }}
        >
          <button
            className="absolute top-2 right-2 z-10 text-xs px-2 py-1 border border-border text-foreground-secondary bg-background-elevated/90 backdrop-blur-sm shadow-card hover:bg-border-hover disabled:opacity-30 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent"
            disabled={data.length === 0 || pinnedSnapshots.length >= 8}
            onClick={handlePin}
            title="Pin current chart as snapshot"
            aria-label="Pin current chart as snapshot"
          >
            + Pin
          </button>
        </div>
        <div
          ref={tooltipRef}
          suppressHydrationWarning
          style={{
            ...TOOLTIP_STYLE,
            backgroundColor: colors.backgroundElevated,
            border: `1px solid ${colors.chartGrid}`,
            color: colors.chartText,
          }}
        />
      </div>
    </div>
  );
}
