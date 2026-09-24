import type { BacktestRunResponse, SignalPayload } from '@/types';

export function selectSignalSource(state: { result: BacktestRunResponse | null; selectedSignalAlphaIndex: number | null }): SignalPayload | undefined {
  const result = state.result;
  if (!result) return undefined;
  const index = state.selectedSignalAlphaIndex;
  if (index === -1) return result.combined?.heatmap ? result.combined : result;
  if (index !== null) return result.alphas[index];
  return result.combined?.heatmap ? result.combined : result.heatmap ? result : result.alphas[0];
}

/** Nearest sampled timestamp at or before `timestamp` (ISO strings sort lexically); first row when earlier than all. */
export function nearestTimestamp(timestamps: string[], timestamp: string): string | null {
  if (timestamps.length === 0) return null;
  let lo = 0, hi = timestamps.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (timestamps[mid] <= timestamp) lo = mid + 1; else hi = mid;
  }
  return timestamps[Math.max(0, lo - 1)];
}

export function selectSignalCell(source: SignalPayload | undefined, symbol: string | null = null, timestamp: string | null = null) {
  const heatmap = source?.heatmap;
  return {
    selectedSymbol: symbol && heatmap?.symbols.includes(symbol) ? symbol : heatmap?.symbols[0] ?? null,
    selectedTimestamp: timestamp && heatmap ? nearestTimestamp(heatmap.timestamps, timestamp) : heatmap?.timestamps[0] ?? null,
  };
}
