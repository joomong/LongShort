'use client';

import React, { useMemo, useCallback, useEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { useAlphaStore } from '@/store/alphaStore';
import { selectSignalSource } from '@/lib/signalSelection';
import { symbolLabel } from '@/lib/symbolLabels';
import { runSimulate } from '@/lib/api';
import { useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { getNextColor } from '@/lib/snapshotColors';
import {
  toMultiSeriesData,
  buildUPlotAxes,
} from '@/lib/uplotUtils';
import { chartInteraction } from '@/lib/chartInteraction';
import { useContainerSize, useUPlot } from '@/lib/uplotAdapters';

interface ChartDataPoint {
  date: string;
  value: number | null;
}

// ── Stable constants ──────────────────────────────────────────────────
const EMPTY_DATA: ChartDataPoint[] = [];
const ROW_HEIGHT = 22;

function formatSignalValue(n: number): string {
  const abs = Math.abs(n);
  return abs >= 1000 ? n.toFixed(1) : abs >= 1 ? n.toFixed(3) : n.toFixed(6);
}

// ── Extracted data table (isolated from chart re-renders) ──
const SignalDataTable = React.memo(function SignalDataTable({
  data,
  selectedSymbol,
  symbolLabels,
}: {
  data: ChartDataPoint[];
  selectedSymbol: string | null;
  symbolLabels: Record<string, string> | null;
}) {
  const tableScrollRef = useRef<HTMLDivElement>(null);
  const tableSize = useContainerSize(tableScrollRef);
  const [scrollTop, setScrollTop] = useState(0);
  const firstVisible = Math.max(0, Math.min(data.length - 1, Math.floor(Math.max(0, scrollTop - ROW_HEIGHT) / ROW_HEIGHT) - 6));
  const lastVisible = Math.min(data.length, firstVisible + Math.ceil(tableSize.height / ROW_HEIGHT) + 14);

  const firstDataIndex = useMemo(() => {
    for (let i = 0; i < data.length; i++) {
      if (data[i].value !== null) return i;
    }
    return 0;
  }, [data]);

  useEffect(() => {
    if (tableScrollRef.current) {
      const top = Math.max(0, (firstDataIndex - 3) * ROW_HEIGHT);
      tableScrollRef.current.scrollTop = top;
      setScrollTop(tableScrollRef.current.scrollTop);
    }
  }, [firstDataIndex, selectedSymbol]);

  return (
    <div className="w-[190px] shrink-0 border-r border-border overflow-hidden flex flex-col">
      <div className="px-2 py-1 border-b border-border bg-background-secondary">
        <span className="text-[11px] text-foreground-secondary font-medium">
          {selectedSymbol ? symbolLabel(selectedSymbol, symbolLabels) : 'signal'} data
        </span>
      </div>
      <div ref={tableScrollRef} className="flex-1 overflow-y-auto" onScroll={e => setScrollTop(e.currentTarget.scrollTop)}>
        <table aria-label="Signal data" aria-rowcount={data.length + 1} className="w-full text-[11px] border-collapse font-mono">
          <thead className="sticky top-0 bg-background-secondary">
            <tr className="border-b border-border-strong">
              <th className="text-left px-2 py-0.5 text-foreground-secondary font-medium">dates</th>
              <th className="text-right px-2 py-0.5 text-foreground-secondary font-medium">signal</th>
            </tr>
          </thead>
          <tbody>
            {firstVisible > 0 && <tr aria-hidden="true"><td colSpan={2} style={{ height: firstVisible * ROW_HEIGHT, padding: 0, border: 0 }} /></tr>}
            {data.slice(firstVisible, lastVisible).map((row, index) => {
              const v = row.value;
              const hasValue = v !== null && v !== undefined;
              return (
                <tr key={row.date} aria-rowindex={firstVisible + index + 2} className="border-b border-border/20 hover:bg-row-hover" style={{ height: ROW_HEIGHT }}>
                  <td className="px-2 py-px tabular-nums text-foreground-muted whitespace-nowrap">{row.date}</td>
                  <td className={`px-2 py-px tabular-nums text-right whitespace-nowrap ${hasValue ? 'text-data-color' : 'text-foreground-muted'}`}>
                    {hasValue ? v.toFixed(4) : 'NaN'}
                  </td>
                </tr>
              );
            })}
            {lastVisible < data.length && <tr aria-hidden="true"><td colSpan={2} style={{ height: (data.length - lastVisible) * ROW_HEIGHT, padding: 0, border: 0 }} /></tr>}
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
  const symbolLabels = useAlphaStore(s => s.config?.symbolLabels ?? null);
  // Full (unsampled) series for the selected symbol: the heatmap payload is date-sampled
  // for wide panels, so the chart re-asks the engine for this one column at every date.
  const result = useAlphaStore(s => s.result);
  const revision = useAlphaStore(s => s.resultRevision);
  const selectedSignalAlphaIndex = useAlphaStore(s => s.selectedSignalAlphaIndex);
  const currentSettings = useAlphaStore(s => s.settings);
  const [fullSeries, setFullSeries] = useState<{ key: string; timestamps: string[]; values: (number | null)[] } | null>(null);
  const seriesCache = useRef(new Map<string, NonNullable<typeof fullSeries>>());
  useEffect(() => { seriesCache.current.clear(); }, [revision]);
  const [seriesState, setSeriesState] = useState<'idle' | 'loading' | 'error'>('idle');
  const needsFull = !!heatmap?.sampled && (heatmap.rowStride ?? 1) > 1 && !!selectedSymbol;
  const alphaIndex = selectedSignalAlphaIndex ?? (result?.combined ? -1 : 0);
  const alphaXml = alphaIndex >= 0 ? result?.alphas[alphaIndex]?.xml : undefined;
  const seriesKey = `${revision}:${alphaIndex}:${selectedSymbol ?? ''}`;
  useEffect(() => {
    if (!needsFull || !selectedSymbol || !alphaXml || fullSeries?.key === seriesKey) return;
    const cached = seriesCache.current.get(seriesKey);
    if (cached) {
      seriesCache.current.delete(seriesKey);
      seriesCache.current.set(seriesKey, cached);
      setFullSeries(cached);
      setSeriesState('idle');
      return;
    }
    const { simStart, isStart, osStart } = result?.runSettings ?? currentSettings;
    const mode = result?.runSettings?.mode ?? (currentSettings.barInterval === 'D0' ? 0 : 1);
    const threads = result?.runSettings?.threads ?? currentSettings.nJobs;
    const controller = new AbortController();
    let active = true;
    setSeriesState('loading');
    runSimulate(alphaXml, simStart, isStart, osStart, controller.signal, threads, mode, { signalPlotSymbols: [selectedSymbol] })
      .then(response => {
        if (!active) return;
        const plot = response.batches[0]?.signalPlot ?? response.signalPlot;
        if (!plot || plot.symbols[0] !== selectedSymbol) throw new Error('no series');
        const series = { key: seriesKey, timestamps: plot.timestamps, values: plot.values.map(row => row[0] ?? null) };
        seriesCache.current.set(seriesKey, series);
        // Bound memory; a new run invalidates every cached series.
        if (seriesCache.current.size > 32) seriesCache.current.delete(seriesCache.current.keys().next().value!);
        setFullSeries(series);
        setSeriesState('idle');
      })
      .catch(() => { if (active && !controller.signal.aborted) setSeriesState('error'); });
    return () => { active = false; controller.abort(); };
  }, [needsFull, selectedSymbol, alphaXml, seriesKey, fullSeries?.key, result?.runSettings, currentSettings]);
  const pinnedSnapshots = useAlphaStore(useShallow(s => s.pinnedSnapshots));
  const addSnapshot = useAlphaStore(s => s.addSnapshot);
  const theme = useThemeStore(s => s.theme);
  const containerRef = useRef<HTMLDivElement>(null);

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
    if (needsFull && fullSeries?.key === seriesKey) {
      return fullSeries.timestamps.map((ts, i) => {
        const v = fullSeries.values[i];
        return { date: ts, value: typeof v === 'number' && Number.isFinite(v) ? v : null };
      });
    }
    const symbolIdx = symbolMap.get(selectedSymbol);
    if (symbolIdx === undefined) return EMPTY_DATA;
    return heatmap.timestamps.map((ts, timeIdx) => {
      const v = (heatmap.values as unknown as Array<Array<number | null | undefined>>)[timeIdx]?.[symbolIdx];
      return {
        date: ts,
        value: typeof v === 'number' && Number.isFinite(v) ? v : null,
      };
    });
  }, [heatmap, selectedSymbol, symbolMap, needsFull, fullSeries, seriesKey]);

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

  // ── uPlot options (memoized to avoid unnecessary recreation) ──
  const opts = useMemo<uPlot.Options | null>(() => {
    const [axisX, axisY] = buildUPlotAxes(colors);

    const seriesDefs: uPlot.Series[] = [
      {},
      {
        stroke: colors.chartLine,
        width: 1.25,
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
      width: 1,
      height: 1,
      padding: [10, 30, 0, 0],
      cursor: {
        drag: { x: false, y: false },
      },
      legend: { show: false },
      plugins: [chartInteraction(formatSignalValue)],
      scales: {
        x: { time: true },
        y: { auto: true },
      },
      axes: [
        {
          ...axisX,
          gap: 5,
        },
        {
          ...axisY,
          values: (_u: uPlot, vals: number[]) => vals.map(v => v.toFixed(3)),
          size: 60,
          grid: { ...axisY.grid, dash: [3, 3] },
        },
      ],
      series: seriesDefs,
    };
  }, [colors, pinnedSnapshots]);

  useUPlot(containerRef, opts, uData, size, seriesKey);

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
    <div className="flex-[1_1_0%] flex flex-row bg-chart-background min-h-[240px]">
      {/* Left: STAR-style data table (memo-isolated from chart re-renders) */}
      <SignalDataTable data={data} selectedSymbol={selectedSymbol} symbolLabels={symbolLabels} />

      {/* Right: uPlot chart */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
        {needsFull && seriesState !== 'idle' ? (
          <div className="absolute top-1 right-2 z-10 text-[10px] text-foreground-muted">
            {seriesState === 'loading' ? 'loading full series…' : 'full series unavailable (showing sampled dates)'}
          </div>
        ) : null}
        <div
          ref={containerRef}
          className="relative flex-1 w-full min-h-0 p-2 select-none overflow-hidden"
          style={{ cursor: hasData ? 'grab' : 'default' }}
        >
          <button
            className="absolute top-2 right-2 z-10 text-[11px] px-2 py-0.5 border border-border text-foreground-secondary bg-background hover:bg-row-hover disabled:opacity-30 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent"
            disabled={data.length === 0 || pinnedSnapshots.length >= 8}
            onClick={handlePin}
            title="Pin current chart as snapshot"
            aria-label="Pin current chart as snapshot"
          >
            + Pin
          </button>
        </div>
      </div>
    </div>
  );
}
