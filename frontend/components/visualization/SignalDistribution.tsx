'use client';

import { useAlphaStore } from '@/store/alphaStore';

function formatSignalValue(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toFixed(0);
  if (abs >= 1) return v.toFixed(2);
  if (abs >= 0.001) return v.toFixed(4);
  return v.toExponential(2);
}

export default function SignalDistribution() {
  const { result } = useAlphaStore();

  const min = result?.signalRange?.min;
  const max = result?.signalRange?.max;
  const distribution = result?.distribution;
  const maxCount = distribution ? Math.max(...distribution) : 0;

  return (
    <div className="px-4 py-2 bg-background-tertiary border-b border-border">
      {/* Range Info */}
      <div className="flex justify-end items-center mb-1">
        <div className="flex items-center gap-3 text-xs font-mono tabular-nums">
          <span className="text-foreground-secondary">{min != null ? formatSignalValue(min) : '—'}</span>
          <span className="text-foreground-muted">~</span>
          <span className="text-foreground-secondary">{max != null ? formatSignalValue(max) : '—'}</span>
        </div>
      </div>

      {/* Distribution Histogram - Gray, compact */}
      <div className="flex items-end h-8">
        {distribution ? distribution.map((count, i) => {
          const height = maxCount > 0 ? (count / maxCount) * 100 : 0;

          return (
            <div
              key={i}
              className="flex-1 bg-foreground-muted"
              style={{
                height: `${height}%`,
                marginRight: i < distribution.length - 1 ? '1px' : '0',
              }}
            />
          );
        }) : null}
      </div>
    </div>
  );
}
