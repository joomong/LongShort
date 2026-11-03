import type { SignalPlot } from '../../src/types';

/** Independent fixture statistics for generated signal matrices. */
export function signalFixture(plot: SignalPlot): SignalPlot {
  const centers: (number | null)[] = [],
    scales: (number | null)[] = [];
  const median = (sorted: number[]) =>
    sorted.length % 2
      ? sorted[sorted.length >> 1]
      : sorted[sorted.length / 2 - 1] / 2 + sorted[sorted.length / 2] / 2;
  for (const row of plot.values) {
    const finite = row
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
      .sort((a, b) => a - b);
    if (!finite.length) {
      centers.push(null);
      scales.push(null);
      continue;
    }
    const center = median(finite);
    let scale = median(finite.map((v) => Math.abs(v - center)).sort((a, b) => a - b)) * 1.4826;
    if (scale === 0 && finite[0] !== finite.at(-1)) {
      let mean = 0,
        squared = 0;
      finite.forEach((v, i) => {
        const delta = v - mean;
        mean += delta / (i + 1);
        squared += delta * (v - mean);
      });
      scale = Math.sqrt(squared / finite.length);
    }
    centers.push(center);
    scales.push(scale);
  }
  return { ...plot, centers, scales };
}
