'use client';

import { useMemo, useRef } from 'react';
import type uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { useThemeStore } from '@/theme';
import { useRunStore } from '@/runStore';
import { alignedSeries, buildUPlotAxes, chartInteraction, useContainerSize, useUPlot } from '@/chart';
import { getThemeColors } from '@/chartStyle';
import { formatPercent, MILLION_DOLLARS } from '@/format';
import type { AlphaResult, RunContext } from '@/types';

export type DetailMetricKey = 'pnl' | 'turnover' | 'nonZero' | 'maxWeight' | 'mdd';
const IS_COLOR = '#df8e13', OS_COLOR = '#258249';
const EMPTY: uPlot.AlignedData = [[], []];
const LABELS: Record<DetailMetricKey, string> = {
  pnl: 'PnL ($M)', turnover: 'Turnover (%)', nonZero: 'Non-zero Position', maxWeight: 'Max Weight (%)', mdd: 'Drawdown (%)',
};

export default function PerformanceChart({ alpha, metric, context }: { alpha: AlphaResult; metric: DetailMetricKey; context: RunContext }) {
  const active = useRunStore(state => state.viewMode === 'detail');
  const revision = useRunStore(state => state.resultRevision);
  const theme = useThemeStore(state => state.theme);
  const colors = useMemo(() => getThemeColors(theme), [theme]);
  const container = useRef<HTMLDivElement>(null);
  const size = useContainerSize(container);
  const series = alpha.performance;
  const data = useMemo<uPlot.AlignedData>(() => {
    if (!active || !series) return EMPTY;
    const columns = metric === 'pnl' ? [series.cumulativeNet, series.cumulativeGross]
      : [metric === 'turnover' ? series.turnover : metric === 'nonZero' ? series.positionCount
        : metric === 'maxWeight' ? series.maxWeight : series.drawdown];
    return alignedSeries(series.timestamps, columns);
  }, [active, series, metric]);
  const osTime = series && series.osStartIndex < series.timestamps.length ? series.timestamps[series.osStartIndex] / 1000 : null;
  const formatter = useMemo(() => metric === 'pnl'
    ? (value: number) => `$${(value * context.reportNotional / MILLION_DOLLARS).toFixed(2)}M`
    : metric === 'nonZero' ? (value: number) => value.toFixed(0)
      : (value: number) => formatPercent(value, metric === 'turnover' ? 1 : 2), [metric, context.reportNotional]);
  const options = useMemo<uPlot.Options | null>(() => {
    if (!series?.timestamps.length) return null;
    const [xAxis, yAxis] = buildUPlotAxes(colors);
    const stroke = (plot: uPlot) => {
      if (osTime === null) return IS_COLOR;
      const split = plot.valToPos(osTime, 'x', true);
      const { left, width } = plot.bbox;
      if (split <= left) return OS_COLOR;
      if (split >= left + width || width <= 0) return IS_COLOR;
      const gradient = plot.ctx.createLinearGradient(left, 0, left + width, 0);
      const fraction = (split - left) / width;
      gradient.addColorStop(0, IS_COLOR); gradient.addColorStop(fraction, IS_COLOR);
      gradient.addColorStop(fraction, OS_COLOR); gradient.addColorStop(1, OS_COLOR);
      return gradient;
    };
    return {
      width: 1, height: 1, padding: [8, 10, 0, 0], legend: { show: false },
      cursor: { y: false, drag: { x: false, y: false } },
      plugins: [chartInteraction(formatter, false)], scales: { x: { time: true }, y: { auto: true } },
      axes: [{ ...xAxis, gap: 5, size: 40 }, { ...yAxis, size: 70,
        values: (_plot, values) => values.map(value => metric === 'pnl'
          ? (value * context.reportNotional / MILLION_DOLLARS).toFixed(2)
          : metric === 'nonZero' ? value.toFixed(0) : (value * 100).toFixed(2)) }],
      series: metric === 'pnl'
        ? [{}, { stroke, width: 2, label: 'Net', dash: [4, 3], points: { show: false } }, { stroke, width: 2, label: 'Gross', points: { show: false } }]
        : [{}, { stroke, width: 2, label: LABELS[metric], points: { show: false } }],
      hooks: { draw: [plot => {
        if (osTime === null) return;
        const x = plot.valToPos(osTime, 'x', true);
        if (x < plot.bbox.left || x > plot.bbox.left + plot.bbox.width) return;
        const ctx = plot.ctx; ctx.save(); ctx.strokeStyle = colors.chartGrid; ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x, plot.bbox.top);
        ctx.lineTo(x, plot.bbox.top + plot.bbox.height); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = colors.chartText; ctx.font = '10px monospace'; ctx.fillText('OS', x + 4, plot.bbox.top + 12); ctx.restore();
      }] },
    };
  }, [series?.timestamps.length, colors, osTime, metric, formatter, context.reportNotional]);
  useUPlot(container, options, data, size, `${revision}:${alpha.id}`, active);
  return <div className="h-full min-h-0 min-w-0 flex flex-col relative">
    <div className="flex shrink-0 items-center gap-3 px-2 py-1 overflow-x-auto whitespace-nowrap text-[10px] text-foreground-muted">
      <span>{LABELS[metric]}</span>
      {metric === 'pnl' ? <><span>━ Gross</span><span>┄ Net</span></> : null}
      <span style={{ color: IS_COLOR }}>━ IS</span>{osTime !== null ? <span style={{ color: OS_COLOR }}>━ OS</span> : null}
    </div>
    <div ref={container} className="flex-1 min-h-0 min-w-0 relative" />
  </div>;
}
