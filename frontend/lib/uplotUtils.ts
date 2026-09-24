import type React from 'react';
import type uPlot from 'uplot';
import type { ThemeColors } from './themeColors';

export function dateStrToUnix(date: string): number {
  return Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(date) ? date + 'T00:00:00Z' : date) / 1000;
}
export const fmtDate = (seconds: number): string => Number.isFinite(seconds) ? new Date(seconds * 1000).toISOString().slice(0, 10) : '';
export const fmtMonth = (seconds: number): string => fmtDate(seconds).slice(0, 7);
export function timeAxisValues(plot: uPlot, values: number[]): string[] {
  const span = (plot.scales.x.max ?? 0) - (plot.scales.x.min ?? 0);
  return values.map(v => span > 86400 * 365 * 4 ? fmtDate(v).slice(0, 4) : span > 86400 * 120 ? fmtMonth(v) : fmtDate(v));
}
export const getDPR = (): number => typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1;
const finiteValue = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null;

export function toMultiSeriesData(points: readonly Record<string, unknown>[], dateKey: string, seriesKeys: readonly string[]): uPlot.AlignedData {
  const timestamps: number[] = [];
  const values: (number | null)[][] = seriesKeys.map(() => []);
  for (const point of points) {
    const date = point[dateKey];
    const timestamp = typeof date === 'string' ? dateStrToUnix(date) : NaN;
    if (!Number.isFinite(timestamp)) continue;
    timestamps.push(timestamp);
    seriesKeys.forEach((key, index) => values[index].push(finiteValue(point[key])));
  }
  return [timestamps, ...values];
}
export function toAlignedData(points: readonly { date: string; value: number | null }[]): uPlot.AlignedData {
  return toMultiSeriesData(points, 'date', ['value']);
}
export function toIsOsAlignedData(points: readonly { date: string; isNet?: number; osNet?: number }[]): uPlot.AlignedData {
  return toMultiSeriesData(points, 'date', ['isNet', 'osNet']);
}
export function buildUPlotAxes(colors: ThemeColors): [uPlot.Axis, uPlot.Axis] {
  const shared: uPlot.Axis = {
    stroke: colors.chartText, font: '11px monospace',
    grid: { stroke: colors.chartGrid, width: 1 },
    ticks: { stroke: colors.chartGrid, width: 1 },
  };
  return [{ ...shared, values: timeAxisValues, space: 100, size: 44 }, { ...shared, size: 55 }];
}
export const TOOLTIP_STYLE: React.CSSProperties = {
  position: 'absolute', display: 'none', pointerEvents: 'none', zIndex: 20,
  padding: '6px 8px', font: '11px monospace', whiteSpace: 'nowrap',
};
