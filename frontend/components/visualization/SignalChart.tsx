'use client';

import { useMemo, useState, useCallback, useEffect, useRef } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from 'recharts';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { getNextColor } from '@/lib/snapshotColors';

interface ChartDataPoint {
  date: string;
  value: number | null;
}

// ── Stable constants (avoid new references per render) ──────────────────
const EMPTY_DATA: ChartDataPoint[] = [];
const CHART_MARGIN = { top: 10, right: 30, left: 0, bottom: 10 };
const AUTO_DOMAIN: [string, string] = ['auto', 'auto'];

// ── Stable formatter functions ──────────────────────────────────────────
function formatXTick(dateStr: string) {
  const d = new Date(dateStr);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

function formatYTick(value: number) {
  return value.toFixed(3);
}

function formatTooltipLabel(dateStr: string) {
  const d = new Date(dateStr);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}

function formatTooltipValue(val: unknown, name: unknown) {
  if (val === null || val === undefined) {
    return ['N/A', name === 'value' ? 'Signal' : String(name)];
  }
  const n = typeof val === 'number' ? val : Number(val);
  if (!isFinite(n)) {
    return ['N/A', name === 'value' ? 'Signal' : String(name)];
  }
  const label = name === 'value' ? 'Signal' : String(name);
  const abs = Math.abs(n);
  const formatted = abs >= 1000 ? n.toFixed(1) : abs >= 1 ? n.toFixed(3) : n.toFixed(6);
  return [formatted, label];
}

export default function SignalChart() {
  // Granular selectors
  const heatmap = useAlphaStore(s => s.result?.heatmap);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const pinnedSnapshots = useAlphaStore(s => s.pinnedSnapshots);
  const addSnapshot = useAlphaStore(s => s.addSnapshot);
  const { theme } = useThemeStore();
  const containerRef = useRef<HTMLDivElement>(null);

  // ── Measure container size with ResizeObserver (replaces ResponsiveContainer) ──
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      // Round to avoid sub-pixel oscillation loops
      const w = Math.floor(width);
      const h = Math.floor(height);
      setSize(prev => (prev.width === w && prev.height === h) ? prev : { width: w, height: h });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // View range state (X-axis: indices, Y-axis: values)
  const [viewRange, setViewRange] = useState<{ start: number; end: number } | null>(null);
  const [yViewRange, setYViewRange] = useState<{ min: number; max: number } | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState<{
    x: number;
    y: number;
    xRange: { start: number; end: number };
    yRange: { min: number; max: number };
  } | null>(null);

  // Refs to avoid stale closures in stable callbacks
  const viewRangeRef = useRef(viewRange);
  viewRangeRef.current = viewRange;
  const yViewRangeRef = useRef(yViewRange);
  yViewRangeRef.current = yViewRange;
  const dataLenRef = useRef(0);
  const dataYRangeRef = useRef({ min: -1, max: 1 });
  const isDraggingRef = useRef(false);
  isDraggingRef.current = isDragging;
  const dragStartRef = useRef(dragStart);
  dragStartRef.current = dragStart;

  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => { void theme; return getThemeColors(); }, [theme]);

  // Memoize Recharts style objects (avoid new refs each render)
  const xTickStyle = useMemo(() => (
    { fill: colors.chartText, fontSize: 11, fontFamily: 'var(--font-mono), monospace' }
  ), [colors.chartText]);
  const xTickLineStyle = useMemo(() => ({ stroke: colors.chartGrid }), [colors.chartGrid]);
  const yTickStyle = xTickStyle;
  const yTickLineStyle = xTickLineStyle;
  const tooltipStyle = useMemo(() => ({
    backgroundColor: colors.backgroundElevated,
    border: `1px solid ${colors.chartGrid}`,
    borderRadius: 0,
    color: colors.chartText,
    fontSize: 12,
    boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
  }), [colors.backgroundElevated, colors.chartGrid, colors.chartText]);
  const dotStyle = useMemo(() => ({ fill: colors.chartLine, r: 2, strokeWidth: 0 }), [colors.chartLine]);

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
  dataLenRef.current = data.length;

  // Compute data Y range (min/max values)
  const dataYRange = useMemo(() => {
    if (data.length === 0) return { min: -1, max: 1 };

    const values = data
      .map(d => d.value)
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));

    if (values.length === 0) return { min: -1, max: 1 };

    let min = values[0], max = values[0];
    for (let i = 1; i < values.length; i++) {
      if (values[i] < min) min = values[i];
      if (values[i] > max) max = values[i];
    }
    const padding = (max - min) * 0.1 || 0.1;
    return { min: min - padding, max: max + padding };
  }, [data]);
  dataYRangeRef.current = dataYRange;

  // Reset view range when symbol changes
  useEffect(() => {
    setViewRange(null);
    setYViewRange(null);
  }, [selectedSymbol]);

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
    const allDates = Array.from(dateMap.keys()).sort();
    return allDates.map((date) => ({
      date,
      ...dateMap.get(date)!,
    }));
  }, [data, pinnedSnapshots]);

  // Get visible data based on view range
  const visibleData = useMemo(() => {
    if (!viewRange || mergedData.length === 0) return mergedData;
    return mergedData.slice(viewRange.start, viewRange.end + 1);
  }, [mergedData, viewRange]);

  // Y-axis domain (memoized to avoid new array ref each render)
  const yDomain = useMemo<[number, number] | [string, string]>(() => {
    if (yViewRange) return [yViewRange.min, yViewRange.max];
    return AUTO_DOMAIN;
  }, [yViewRange]);

  // X-axis interval
  const xInterval = useMemo(() => {
    return visibleData.length > 0 ? Math.max(0, Math.floor(visibleData.length / 12) - 1) : 0;
  }, [visibleData.length]);

  // Handle pinning current chart as snapshot
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

  // Handle mouse wheel for zoom (stable callback — reads refs, no state deps)
  const handleWheel = useCallback((e: React.WheelEvent) => {
    const len = dataLenRef.current;
    if (len === 0) return;

    const currentRange = viewRangeRef.current ?? { start: 0, end: len - 1 };
    const rangeSize = Math.max(1, currentRange.end - currentRange.start);
    const containerWidth = containerRef.current?.clientWidth ?? 1;

    const isHorizontalPan = Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey;
    if (isHorizontalPan) {
      e.preventDefault();
      const indexDelta = (e.deltaX / containerWidth) * rangeSize;

      let newStart = currentRange.start + indexDelta;
      let newEnd = currentRange.end + indexDelta;

      if (newStart < 0) { newEnd -= newStart; newStart = 0; }
      const maxEnd = len - 1;
      if (newEnd > maxEnd) { newStart -= (newEnd - maxEnd); newEnd = maxEnd; }

      if (newEnd - newStart >= len - 1) {
        setViewRange(null);
      } else {
        setViewRange({ start: Math.max(0, Math.round(newStart)), end: Math.min(maxEnd, Math.round(newEnd)) });
      }
      return;
    }

    e.preventDefault();
    const zoomFactor = e.deltaY > 0 ? 1.1 : 0.9;
    const newRange = Math.max(5, Math.min(len, Math.round(rangeSize * zoomFactor)));
    const center = (currentRange.start + currentRange.end) / 2;

    let newStart = Math.round(center - newRange / 2);
    let newEnd = Math.round(center + newRange / 2);

    if (newStart < 0) { newStart = 0; newEnd = Math.min(len - 1, newRange); }
    if (newEnd >= len) { newEnd = len - 1; newStart = Math.max(0, newEnd - newRange); }

    if (newEnd - newStart >= len - 1) {
      setViewRange(null);
    } else {
      setViewRange({ start: newStart, end: newEnd });
    }
  }, []);

  // Handle mouse down for drag start (stable callback)
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    const len = dataLenRef.current;
    if (len === 0 || e.button !== 0) return;
    e.preventDefault();
    setIsDragging(true);
    setDragStart({
      x: e.clientX,
      y: e.clientY,
      xRange: viewRangeRef.current ?? { start: 0, end: len - 1 },
      yRange: yViewRangeRef.current ?? dataYRangeRef.current,
    });
  }, []);

  // Handle mouse move for dragging (stable callback)
  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    const ds = dragStartRef.current;
    if (!isDraggingRef.current || !ds || !containerRef.current) return;
    const len = dataLenRef.current;
    if (len === 0) return;

    const containerWidth = containerRef.current.clientWidth;
    const containerHeight = containerRef.current.clientHeight;
    const deltaX = e.clientX - ds.x;
    const deltaY = e.clientY - ds.y;

    const xRangeSize = Math.max(1, ds.xRange.end - ds.xRange.start);
    const indexDelta = (deltaX / containerWidth) * xRangeSize * -1;

    let newStart = ds.xRange.start + indexDelta;
    let newEnd = ds.xRange.end + indexDelta;

    if (newStart < 0) { newStart = 0; newEnd = xRangeSize; }
    if (newEnd >= len) { newEnd = len - 1; newStart = newEnd - xRangeSize; }

    setViewRange({ start: Math.max(0, Math.round(newStart)), end: Math.min(len - 1, Math.round(newEnd)) });

    const yRangeSize = ds.yRange.max - ds.yRange.min;
    const valueDelta = (deltaY / containerHeight) * yRangeSize;
    setYViewRange({ min: ds.yRange.min + valueDelta, max: ds.yRange.max + valueDelta });
  }, []);

  // Handle mouse up to stop dragging
  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
    setDragStart(null);
  }, []);

  // Add global mouse up listener
  useEffect(() => {
    if (!isDragging) return;
    const handleGlobalMouseUp = () => {
      setIsDragging(false);
      setDragStart(null);
    };
    window.addEventListener('mouseup', handleGlobalMouseUp);
    return () => window.removeEventListener('mouseup', handleGlobalMouseUp);
  }, [isDragging]);

  // Cursor style (memoized)
  const cursorStyle = useMemo(() => {
    const cursor = visibleData.length > 0 ? (isDragging ? 'grabbing' : 'grab') : 'default';
    return { cursor };
  }, [visibleData.length, isDragging]);

  const hasData = visibleData.length > 0;

  return (
    <div className="flex-1 flex flex-col bg-background-tertiary min-h-0 panel-inset">
      <div
        ref={containerRef}
        className="relative flex-1 w-full min-h-0 p-2 select-none overflow-hidden"
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        style={cursorStyle}
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
        {size.width > 0 && size.height > 0 && (
          <LineChart
            width={size.width}
            height={size.height}
            data={hasData ? visibleData : []}
            margin={CHART_MARGIN}
          >
            <CartesianGrid strokeDasharray="3 3" stroke={colors.chartGrid} strokeOpacity={0.5} />
            <XAxis
              dataKey="date"
              stroke={colors.chartText}
              tick={xTickStyle}
              tickLine={xTickLineStyle}
              tickFormatter={formatXTick}
              interval={xInterval}
              minTickGap={40}
            />
            <YAxis
              stroke={colors.chartText}
              tick={yTickStyle}
              tickLine={yTickLineStyle}
              domain={yDomain}
              tickFormatter={formatYTick}
              width={60}
              allowDataOverflow={true}
            />
            {hasData && (
              <>
                <Tooltip
                  contentStyle={tooltipStyle}
                  labelFormatter={formatTooltipLabel}
                  formatter={formatTooltipValue}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="value"
                  stroke={colors.chartLine}
                  dot={dotStyle}
                  strokeWidth={2}
                  isAnimationActive={false}
                />
                {pinnedSnapshots.map((snap) => (
                  <Line
                    key={snap.id}
                    type="linear"
                    dataKey={`snap_${snap.id}`}
                    stroke={snap.color}
                    strokeWidth={1.5}
                    dot={false}
                    connectNulls
                    isAnimationActive={false}
                    name={snap.name}
                  />
                ))}
              </>
            )}
          </LineChart>
        )}
      </div>
    </div>
  );
}
