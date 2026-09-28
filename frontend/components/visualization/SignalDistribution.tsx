'use client';

import { useAlphaStore } from '@/store/alphaStore';
import { useCallback, useState } from 'react';
import { selectSignalSource } from '@/lib/signalSelection';
import { symbolLabel } from '@/lib/symbolLabels';
import { SIGNAL_DISTRIBUTION_HEIGHT } from '@/lib/signalPanelLayout';

function formatSignalValue(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toFixed(0);
  if (abs >= 1) return v.toFixed(2);
  if (abs >= 0.001) return v.toFixed(4);
  return v.toExponential(2);
}

export default function SignalDistribution() {
  const signalRange = useAlphaStore(s => selectSignalSource(s)?.signalRange);
  const linearRange = useAlphaStore(s => selectSignalSource(s)?.distributionRange);
  const linearCounts = useAlphaStore(s => selectSignalSource(s)?.distribution);
  const linearBins = useAlphaStore(s => selectSignalSource(s)?.distributionBins);
  const linearExamples = useAlphaStore(s => selectSignalSource(s)?.distributionExamples);
  const logarithmic = useAlphaStore(s => selectSignalSource(s)?.signalDistributionLog);
  const [scale, setScale] = useState<'log' | 'linear'>('log');
  const isLog = scale === 'log' && logarithmic != null;
  const pairedLinear = logarithmic?.linear;
  const distribution = isLog ? logarithmic.counts : pairedLinear?.counts ?? linearCounts;
  const bins = isLog ? logarithmic.bins : pairedLinear?.bins ?? linearBins;
  const distributionRange = isLog ? logarithmic : pairedLinear ?? linearRange;
  const shown = isLog ? logarithmic : pairedLinear;
  const examples = shown ? { symbol: shown.exampleSymbol, date: shown.exampleDate } : linearExamples;
  const exampleSymbols = examples?.symbol;
  const exampleDates = examples?.date;
  const symbolLabels = useAlphaStore(s => s.config?.symbolLabels ?? null);
  const setSelectedCell = useAlphaStore(s => s.setSelectedCell);
  // Click a colour-strip cell (or a bar) -> jump the heatmap/chart to the cell with the largest |value| in that bin.
  const handleBarClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const i = Number(e.currentTarget.dataset.stripBin ?? e.currentTarget.dataset.bin);
    const symbol = exampleSymbols?.[i];
    const date = exampleDates?.[i];
    if (symbol && date) setSelectedCell(symbol, date);
  }, [exampleSymbols, exampleDates, setSelectedCell]);

  const min = distributionRange?.min ?? signalRange?.min;
  const max = distributionRange?.max ?? signalRange?.max;
  // Use reduce instead of Math.max(...arr) to avoid stack overflow on large arrays
  const maxCount = distribution ? distribution.reduce((a, b) => a > b ? a : b, 0) : 0;

  return (
    <div aria-label="Signal distribution" className="box-border flex shrink-0 min-h-0 flex-col px-2 py-1 bg-chart-background border-b border-border" style={{ height: SIGNAL_DISTRIBUTION_HEIGHT }}>
      {/* Range Info */}
      <div className="flex shrink-0 justify-between items-center mb-1">
        <div role="group" aria-label="Histogram scale" className="flex items-center gap-1 text-[10px]">
          <button onClick={() => setScale('log')} disabled={!logarithmic} aria-pressed={isLog}
            className={`px-1 ${isLog ? 'text-foreground underline' : 'text-foreground-muted'} disabled:opacity-40`}
            title={logarithmic ? 'Log-spaced bins over the full value range; negative values and zero are preserved' : 'Run again with the updated engine to load log bins'}>Log</button>
          <button onClick={() => setScale('linear')} aria-pressed={!isLog}
            className={`px-1 ${!isLog ? 'text-foreground underline' : 'text-foreground-muted'}`}>Linear</button>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono tabular-nums" title={signalRange ? `Full value range: ${formatSignalValue(signalRange.min)} to ${formatSignalValue(signalRange.max)}` : undefined}>
          <span className="text-foreground-secondary">{min != null ? formatSignalValue(min) : '—'}</span>
          <span className="text-foreground-muted">~</span>
          <span className="text-foreground-secondary">{max != null ? formatSignalValue(max) : '—'}</span>
        </div>
      </div>

      {/* Preserve every engine-provided bin/count. A 1px floor keeps rare extremes
          visible and clickable; adjacent bins have no artificial gap. */}
      <div className="flex flex-1 min-h-0 items-end">
        {distribution ? distribution.map((count, i) => {
          const height = maxCount > 0 ? (count / maxCount) * 100 : 0;
          const example = exampleSymbols?.[i] && exampleDates?.[i]
            ? `\n${symbolLabel(exampleSymbols![i]!, symbolLabels)} @ ${exampleDates![i]}` : '';
          const center = bins?.[i];
          const edges = shown?.edges;
          const interval = edges && edges.length === (distribution?.length ?? 0) + 1
            ? `${formatSignalValue(edges[i])} – ${formatSignalValue(edges[i + 1])}`
            : center != null ? formatSignalValue(center) : '';
          return (
            <div
              key={i}
              data-bin={i}
              data-count={count}
              title={`${interval}: ${count.toLocaleString()}${example}`}
              onClick={count > 0 ? handleBarClick : undefined}
              className={`flex-1 min-w-0 ${count > 0 ? 'bg-[#737373] dark:bg-[#aaaaaa] cursor-pointer hover:brightness-110' : 'bg-transparent'}`}
              style={{
                height: count > 0 ? `max(1px, ${height}%)` : '0',
              }}
            />
          );
        }) : null}
      </div>
      {/* Click target: the colour strip, one full-height cell per bin (bars are too thin to hit). */}
      <div aria-label="Distribution value scale" className="flex h-3.5 shrink-0 mt-px"
        style={{ background: 'linear-gradient(to right, #7a27d0, #2746d0, #27b4d0, #27c86c, #a9cf27, #e8bf27, #d52b2b)' }}>
        {distribution ? distribution.map((count, i) => (
          <div
            key={i}
            data-strip-bin={i}
            onClick={count > 0 ? handleBarClick : undefined}
            title={count > 0 && exampleSymbols?.[i] && exampleDates?.[i]
              ? `${symbolLabel(exampleSymbols[i]!, symbolLabels)} @ ${exampleDates[i]}` : undefined}
            className={`flex-1 min-w-0 ${count > 0 ? 'cursor-pointer hover:bg-black/20' : ''}`}
          />
        )) : null}
      </div>
    </div>
  );
}
