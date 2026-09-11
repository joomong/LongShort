import type { BacktestRunResponse, SignalPayload } from '@/types';

export function selectSignalSource(state: { result: BacktestRunResponse | null; selectedSignalAlphaIndex: number | null }): SignalPayload | undefined {
  const result = state.result;
  if (!result) return undefined;
  const index = state.selectedSignalAlphaIndex;
  if (index === -1) return result.combined?.heatmap ? result.combined : result;
  if (index !== null) return result.alphas[index];
  return result.combined?.heatmap ? result.combined : result.heatmap ? result : result.alphas[0];
}

export function selectSignalCell(source: SignalPayload | undefined, symbol: string | null = null, timestamp: string | null = null) {
  const heatmap = source?.heatmap;
  return {
    selectedSymbol: symbol && heatmap?.symbols.includes(symbol) ? symbol : heatmap?.symbols[0] ?? null,
    selectedTimestamp: timestamp && heatmap?.timestamps.includes(timestamp) ? timestamp : heatmap?.timestamps[0] ?? null,
  };
}
