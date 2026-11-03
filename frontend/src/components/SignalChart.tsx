'use client';

import React, { useMemo, useCallback, useEffect, useRef, useState } from 'react';
import type uPlot from 'uplot';
import { useShallow } from 'zustand/react/shallow';
import 'uplot/dist/uPlot.min.css';
import { useRunStore, selectSignalSource } from '@/runStore';
import { useThemeStore } from '@/theme';
import { symbolLabel, formatDate, formatSignalTooltip } from '@/format';
import { getThemeColors, getNextColor, OUT_OF_UNIVERSE_HEX } from '@/chartStyle';
import { buildUPlotAxes, chartInteraction, useContainerSize, useUPlot, isMember, splitByMembership } from '@/chart';
import type { PinnedSnapshot } from '@/types';

interface ChartDataPoint {
  date: number;
  value: number | null;
  /** false when the symbol is outside the run's universe on this date. */
  member: boolean;
}

// ── Stable constants ──────────────────────────────────────────────────
const EMPTY_DATA: ChartDataPoint[] = [];
const ROW_HEIGHT = 22;

// ── Extracted data table (isolated from chart re-renders) ──
const SignalDataTable = React.memo(function SignalDataTable({
  data,
  selectedSymbol,
  symbolLabels,
}: {
  data: ChartDataPoint[];
  selectedSymbol: string | null;
  symbolLabels: Record<string, string> | undefined;
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
                  <td className="px-2 py-px tabular-nums text-foreground-muted whitespace-nowrap">{formatDate(row.date)}</td>
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
  const heatmap = useRunStore(state => selectSignalSource(state)?.plot);
  const active = useRunStore(state => state.viewMode !== 'detail' && state.result?.alphas.length === 1);
  const selectedSymbol = useRunStore(state => state.selectedSymbol);
  const symbolLabels = useRunStore(state => state.config?.symbolLabels);
  const revision = useRunStore(state => state.resultRevision);
  const selectedSignalAlphaIndex = useRunStore(state => state.selectedSignalAlphaIndex);
  const hasCombined = useRunStore(state => !!state.result?.combined);
  const fullSeries = useRunStore(state => state.selectedSeries);
  const seriesRequest = useRunStore(state => state.seriesRequest);
  const pinnedSnapshots = useRunStore(state => state.pinnedSnapshots);
  const snapshotColors = useRunStore(useShallow(state => state.pinnedSnapshots.map(snapshot => snapshot.color)));
  const addSnapshot = useRunStore(state => state.addSnapshot);
  const theme = useThemeStore(state => state.theme);
  const container = useRef<HTMLDivElement>(null);
  const size = useContainerSize(container);
  const colors = useMemo(() => getThemeColors(theme), [theme]);
  const alphaIndex = selectedSignalAlphaIndex ?? (hasCombined ? -1 : 0);
  const seriesKey = `${revision}:${alphaIndex}:${selectedSymbol ?? ''}`;
  const needsFull = !!heatmap?.sampled && heatmap.rowStride > 1 && !!selectedSymbol;
  const data = useMemo<ChartDataPoint[]>(() => {
    if (!active || !heatmap || !selectedSymbol) return EMPTY_DATA;
    if (needsFull && fullSeries?.key === seriesKey) {
      return fullSeries.timestamps.map((date, index) => ({ date, value: fullSeries.values[index], member: fullSeries.membership[index] }));
    }
    const column = heatmap.symbols.indexOf(selectedSymbol);
    if (column < 0) return EMPTY_DATA;
    return heatmap.timestamps.map((date, row) => {
      const value = heatmap.values[row]?.[column];
      return { date, value: typeof value === 'number' && Number.isFinite(value) ? value : null, member: isMember(heatmap.membership, row, column) };
    });
  }, [active, heatmap, selectedSymbol, needsFull, fullSeries, seriesKey]);

  const dataCache = useRef<{ base: ChartDataPoint[]; snapshots: PinnedSnapshot['signalSeries'][]; data: uPlot.AlignedData }>();
  const chartData = useMemo<uPlot.AlignedData>(() => {
    const old = dataCache.current;
    if (!active) return old?.data ?? [[], []];
    const sources = pinnedSnapshots.map(snapshot => snapshot.signalSeries);
    if (old?.base === data && old.snapshots.length === sources.length && old.snapshots.every((source, index) => source === sources[index])) return old.data;
    const { inside, outside } = splitByMembership(data.map(point => point.value), data.map(point => point.member));
    const dates = new Set(data.map(point => point.date));
    for (const source of sources) for (const date of source.timestamps) dates.add(date);
    const axis = Array.from(dates).sort((a, b) => a - b);
    const index = new Map(axis.map((date, i) => [date, i]));
    const columns: (number | null)[][] = Array.from({ length: 2 + sources.length }, () => new Array(axis.length).fill(null));
    data.forEach((point, i) => { const target = index.get(point.date)!; columns[0][target] = inside[i]; columns[1][target] = outside[i]; });
    sources.forEach((source, column) => source.timestamps.forEach((date, i) => { columns[column + 2][index.get(date)!] = source.values[i]; }));
    const aligned: uPlot.AlignedData = [axis.map(date => date / 1000), ...columns];
    dataCache.current = { base: data, snapshots: sources, data: aligned };
    return aligned;
  }, [active, data, pinnedSnapshots]);
  const snapshotsRef = useRef(pinnedSnapshots); snapshotsRef.current = pinnedSnapshots;
  const syncSnapshotLabels = useCallback((instance: uPlot) => {
    snapshotsRef.current.forEach((snapshot, index) => {
      if (instance.series[index + 3]) instance.series[index + 3].label = snapshot.name;
    });
  }, []);
  const options = useMemo<uPlot.Options>(() => {
    const [xAxis, yAxis] = buildUPlotAxes(colors);
    return {
      width: 1, height: 1, padding: [10, 30, 0, 0], cursor: { drag: { x: false, y: false } },
      legend: { show: false }, plugins: [chartInteraction(formatSignalTooltip)], scales: { x: { time: true }, y: { auto: true } },
      axes: [{ ...xAxis, gap: 5 }, { ...yAxis, values: (_plot, values) => values.map(value => value.toFixed(3)), size: 60, grid: { ...yAxis.grid, dash: [3, 3] } }],
      series: [{},
        { stroke: colors.chartLine, width: 1.25, label: 'Signal', points: { show: true, size: 4, fill: colors.chartLine, stroke: colors.chartLine } },
        { stroke: OUT_OF_UNIVERSE_HEX, width: 1.25, label: 'Outside universe', points: { show: true, size: 4, fill: OUT_OF_UNIVERSE_HEX, stroke: OUT_OF_UNIVERSE_HEX } },
        ...snapshotColors.map((stroke, index) => ({ stroke, width: 1.5, label: snapshotsRef.current[index].name, spanGaps: true, points: { show: false } } as uPlot.Series))],
      hooks: { ready: [syncSnapshotLabels] },
    };
  }, [colors, snapshotColors, syncSnapshotLabels]);
  const plot = useUPlot(container, options, chartData, size, seriesKey, active);
  useEffect(() => {
    if (plot.current) syncSnapshotLabels(plot.current);
  }, [pinnedSnapshots, plot, syncSnapshotLabels]);
  const pin = useCallback(() => {
    if (!data.length) return;
    addSnapshot({ id: Date.now().toString(36), name: selectedSymbol ?? `Snapshot ${pinnedSnapshots.length + 1}`,
      color: getNextColor(pinnedSnapshots.map(snapshot => snapshot.color)),
      signalSeries: { timestamps: data.map(point => point.date), values: data.map(point => point.value) } });
  }, [data, pinnedSnapshots, selectedSymbol, addSnapshot]);
  const hasData = data.some(point => point.value !== null);
  return <div className="flex-[1_1_0%] flex flex-row bg-chart-background min-h-[240px]">
    <SignalDataTable data={data} selectedSymbol={selectedSymbol} symbolLabels={symbolLabels} />
    <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
      {needsFull && seriesRequest.status !== 'ready' ? <div className="absolute top-1 right-2 z-10 text-[10px] text-foreground-muted">
        {seriesRequest.status === 'loading' ? 'loading full series…' : 'full series unavailable (showing sampled dates)'}
      </div> : null}
      <div ref={container} className="relative flex-1 w-full min-h-0 p-2 select-none overflow-hidden" style={{ cursor: hasData ? 'grab' : 'default' }}>
        <button className="absolute top-2 right-2 z-10 text-[11px] px-2 py-0.5 border border-border text-foreground-secondary bg-background hover:bg-row-hover disabled:opacity-30 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent" disabled={!data.length || pinnedSnapshots.length >= 8} onClick={pin} title="Pin current chart as snapshot" aria-label="Pin current chart as snapshot">+ Pin</button>
      </div>
    </div>
  </div>;
}
