'use client';

import { useMemo, useState, useCallback, useEffect, useRef } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
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

export default function SignalChart() {
  // Granular selectors
  const heatmap = useAlphaStore(s => s.result?.heatmap);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const pinnedSnapshots = useAlphaStore(s => s.pinnedSnapshots);
  const addSnapshot = useAlphaStore(s => s.addSnapshot);
  const { theme } = useThemeStore();
  const containerRef = useRef<HTMLDivElement>(null);

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

  // eslint-disable-next-line @typescript-eslint/no-unused-expressions
  const colors = useMemo(() => { void theme; return getThemeColors(); }, [theme]);

  // Build symbol index Map for O(1) lookup (js-index-maps)
  const symbolMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.symbols.map((s, i) => [s, i]));
  }, [heatmap]);

  const data = useMemo<ChartDataPoint[]>(() => {
    if (!heatmap || !selectedSymbol) return [];

    const symbolIdx = symbolMap.get(selectedSymbol);
    if (symbolIdx === undefined) return [];

    return heatmap.timestamps.map((ts, timeIdx) => {
      const v = (heatmap.values as unknown as Array<Array<number | null | undefined>>)[timeIdx]?.[symbolIdx];
      return {
        date: ts,
        // Preserve missing/no-data values (null/undefined) so the chart can render gaps.
        value: typeof v === 'number' && Number.isFinite(v) ? v : null,
      };
    });
  }, [heatmap, selectedSymbol, symbolMap]);

  // Compute data Y range (min/max values)
  const dataYRange = useMemo(() => {
    if (data.length === 0) return { min: -1, max: 1 };

    const values = data
      .map(d => d.value)
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v));

    if (values.length === 0) return { min: -1, max: 1 };

    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.1 || 0.1;
    return { min: min - padding, max: max + padding };
  }, [data]);

  // Reset view range when symbol changes
  useEffect(() => {
    setViewRange(null);
    setYViewRange(null);
  }, [selectedSymbol]);

  // Merge snapshot series into chart data
  const mergedData = useMemo(() => {
    if (pinnedSnapshots.length === 0) return data;
    // Build a map: date → { value, snap_xxx, ... }
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

  // Handle mouse wheel for zoom
  const handleWheel = useCallback((e: React.WheelEvent) => {
    if (data.length === 0) return;

    const currentRange = viewRange ?? { start: 0, end: data.length - 1 };
    const rangeSize = Math.max(1, currentRange.end - currentRange.start);
    const containerWidth = containerRef.current?.clientWidth ?? 1;

    // Trackpad horizontal scroll = pan, vertical wheel = zoom
    const isHorizontalPan = Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey;
    if (isHorizontalPan) {
      e.preventDefault();
      const indexDelta = (e.deltaX / containerWidth) * rangeSize;

      let newStart = currentRange.start + indexDelta;
      let newEnd = currentRange.end + indexDelta;

      if (newStart < 0) {
        newEnd -= newStart;
        newStart = 0;
      }
      const maxEnd = data.length - 1;
      if (newEnd > maxEnd) {
        const overshoot = newEnd - maxEnd;
        newStart -= overshoot;
        newEnd = maxEnd;
      }

      // If we are still showing everything, keep null to avoid lock-in
      if (newEnd - newStart >= data.length - 1) {
        setViewRange(null);
      } else {
        setViewRange({
          start: Math.max(0, Math.round(newStart)),
          end: Math.min(maxEnd, Math.round(newEnd)),
        });
      }
      return;
    }

    e.preventDefault();
    const zoomFactor = e.deltaY > 0 ? 1.1 : 0.9; // Softer zoom for smoother feel
    const currentStart = currentRange.start;
    const currentEnd = currentRange.end;
    const newRange = Math.max(5, Math.min(data.length, Math.round(rangeSize * zoomFactor)));
    const center = (currentStart + currentEnd) / 2;

    let newStart = Math.round(center - newRange / 2);
    let newEnd = Math.round(center + newRange / 2);

    // Clamp to valid range
    if (newStart < 0) {
      newStart = 0;
      newEnd = Math.min(data.length - 1, newRange);
    }
    if (newEnd >= data.length) {
      newEnd = data.length - 1;
      newStart = Math.max(0, newEnd - newRange);
    }

    if (newEnd - newStart >= data.length - 1) {
      setViewRange(null); // Show all data
    } else {
      setViewRange({ start: newStart, end: newEnd });
    }
  }, [data.length, viewRange]);

  // Handle mouse down for drag start
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (data.length === 0 || e.button !== 0) return;
    e.preventDefault();
    setIsDragging(true);
    setDragStart({
      x: e.clientX,
      y: e.clientY,
      xRange: viewRange ?? { start: 0, end: data.length - 1 },
      yRange: yViewRange ?? dataYRange,
    });
  }, [data.length, viewRange, yViewRange, dataYRange]);

  // Handle mouse move for dragging
  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isDragging || !dragStart || !containerRef.current || data.length === 0) return;

    const containerWidth = containerRef.current.clientWidth;
    const containerHeight = containerRef.current.clientHeight;
    const deltaX = e.clientX - dragStart.x;
    const deltaY = e.clientY - dragStart.y;

    // X-axis panning
    const xRangeSize = Math.max(1, dragStart.xRange.end - dragStart.xRange.start);
    const indexDelta = (deltaX / containerWidth) * xRangeSize * -1;

    let newStart = dragStart.xRange.start + indexDelta;
    let newEnd = dragStart.xRange.end + indexDelta;

    if (newStart < 0) {
      newStart = 0;
      newEnd = xRangeSize;
    }
    if (newEnd >= data.length) {
      newEnd = data.length - 1;
      newStart = newEnd - xRangeSize;
    }

    setViewRange({
      start: Math.max(0, Math.round(newStart)),
      end: Math.min(data.length - 1, Math.round(newEnd))
    });

    // Y-axis panning (inverted: drag up = see higher values)
    const yRangeSize = dragStart.yRange.max - dragStart.yRange.min;
    const valueDelta = (deltaY / containerHeight) * yRangeSize;

    const newMin = dragStart.yRange.min + valueDelta;
    const newMax = dragStart.yRange.max + valueDelta;

    setYViewRange({ min: newMin, max: newMax });
  }, [isDragging, dragStart, data.length]);

  // Handle mouse up to stop dragging
  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
    setDragStart(null);
  }, []);

  // Add global mouse up listener
  useEffect(() => {
    if (isDragging) {
      const handleGlobalMouseUp = () => {
        setIsDragging(false);
        setDragStart(null);
      };
      window.addEventListener('mouseup', handleGlobalMouseUp);
      return () => window.removeEventListener('mouseup', handleGlobalMouseUp);
    }
  }, [isDragging]);

  return (
    <div className="flex-1 flex flex-col bg-background-tertiary min-h-0">
      <div
        ref={containerRef}
        className="relative flex-1 w-full min-h-0 p-2 select-none overflow-hidden"
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        style={{ cursor: visibleData.length > 0 ? (isDragging ? 'grabbing' : 'grab') : 'default' }}
      >
        <button
          className="absolute top-2 right-2 z-10 text-xs px-1.5 py-0.5 rounded border border-border text-foreground-secondary bg-background-tertiary/80 backdrop-blur-sm hover:bg-background-secondary disabled:opacity-30 disabled:cursor-not-allowed"
          disabled={data.length === 0 || pinnedSnapshots.length >= 8}
          onClick={handlePin}
          title="Pin current chart as snapshot"
        >
          + Pin
        </button>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={visibleData.length > 0 ? visibleData : []} margin={{ top: 10, right: 30, left: 0, bottom: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={colors.chartGrid} />
            <XAxis
              dataKey="date"
              stroke={colors.chartText}
              tick={{ fill: colors.chartText, fontSize: 10, fontFamily: 'var(--font-mono), monospace' }}
              tickLine={{ stroke: colors.chartGrid }}
              tickFormatter={(dateStr: string) => {
                const d = new Date(dateStr);
                return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
              }}
              interval={visibleData.length > 0 ? Math.max(0, Math.floor(visibleData.length / 12) - 1) : 0}
              minTickGap={40}
            />
            <YAxis
              stroke={colors.chartText}
              tick={{ fill: colors.chartText, fontSize: 10, fontFamily: 'var(--font-mono), monospace' }}
              tickLine={{ stroke: colors.chartGrid }}
              domain={yViewRange ? [yViewRange.min, yViewRange.max] : ['auto', 'auto']}
              tickFormatter={(value: number) => value.toFixed(3)}
              width={60}
              allowDataOverflow={true}
            />
            {visibleData.length > 0 && (
              <>
                <Tooltip
                  contentStyle={{
                    backgroundColor: colors.chartBackground,
                    border: `1px solid ${colors.chartGrid}`,
                    borderRadius: 4,
                    color: colors.chartText,
                    fontSize: 12,
                  }}
                  labelFormatter={(dateStr: string) => {
                    const d = new Date(dateStr);
                    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
                  }}
                  formatter={(val, name) => {
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
                  }}
                  isAnimationActive={false}
                />
                <Line
                  type="linear"
                  dataKey="value"
                  stroke={colors.chartLine}
                  dot={{ fill: colors.chartLine, r: 3 }}
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
        </ResponsiveContainer>
      </div>
    </div>
  );
}
