export type SignalRows = readonly ArrayLike<number | null | undefined>[];

// Statistics depend on data, not canvas size or theme. Weak keys release old runs.
const cache = new WeakMap<SignalRows, Map<number, Float32Array[]>>();

function median(sorted: readonly number[]): number {
  const middle = sorted.length >> 1;
  return sorted.length % 2 ? sorted[middle] : sorted[middle - 1] / 2 + sorted[middle] / 2;
}

// Normalize each date across its instruments. Other dates never influence a cell.
// Date sampling does not change this comparison; symbol sampling uses the received subset.
export function robustCrossSection(values: SignalRows, symbolCount: number): Float32Array[] {
  const cached = cache.get(values)?.get(symbolCount);
  if (cached) return cached;
  const zRows = Array.from({ length: values.length }, () => new Float32Array(symbolCount).fill(NaN));

  for (let time = 0; time < values.length; time++) {
    const row = values[time];
    const finite: number[] = [];
    for (let symbol = 0; symbol < symbolCount; symbol++) {
      const value = row?.[symbol];
      if (typeof value === 'number' && Number.isFinite(value)) finite.push(value);
    }
    if (finite.length === 0) continue;

    finite.sort((a, b) => a - b);
    const center = median(finite);
    const deviations = finite.map(value => Math.abs(value - center)).sort((a, b) => a - b);
    let scale = median(deviations) * 1.4826;
    if (scale === 0 && finite[0] !== finite[finite.length - 1]) {
      // A mostly equal cross-section can have zero MAD despite genuine outliers.
      // Population standard deviation retains them; keep the robust median center.
      let mean = 0;
      let squaredDifferences = 0;
      for (let i = 0; i < finite.length; i++) {
        const delta = finite[i] - mean;
        mean += delta / (i + 1);
        squaredDifferences += delta * (finite[i] - mean);
      }
      scale = Math.sqrt(squaredDifferences / finite.length);
    }

    for (let symbol = 0; symbol < symbolCount; symbol++) {
      const value = row?.[symbol];
      if (typeof value === 'number' && Number.isFinite(value)) {
        zRows[time][symbol] = scale > 0 ? (value - center) / scale : 0;
      }
    }
  }

  const bySize = cache.get(values) ?? new Map<number, Float32Array[]>();
  bySize.set(symbolCount, zRows);
  cache.set(values, bySize);
  return zRows;
}
