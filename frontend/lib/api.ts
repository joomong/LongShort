import type {
  StarConfigResponse,
  StarBatchResponse,
  StarGAEvent,
  StarGACompleteEvent,
  StarSummary,
  StarTimeseries,
  BacktestRunResponse,
  BacktestAlphaResult,
  PeriodMetrics,
  StarSignalPayload,
  SignalPayload,
  AlphaListResponse,
  AlphaDetailResponse,
} from '@/types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

function parseApiErrorPayload(data: unknown): string | undefined {
  if (!data || typeof data !== 'object') return undefined;
  const obj = data as Record<string, unknown>;

  if (typeof obj.detail === 'string') return obj.detail;
  if (typeof obj.error === 'string') return obj.error;
  if (Array.isArray(obj.detail)) {
    return obj.detail
      .map((d: unknown) => {
        if (d && typeof d === 'object' && 'msg' in d) {
          const msg = (d as Record<string, unknown>).msg;
          if (typeof msg === 'string') return msg;
        }
        return JSON.stringify(d);
      })
      .join('; ');
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Adapter helpers
// C++ STAR engine now returns display-ready values: percentage fields as
// percentage points (e.g. 26.7 = 26.7%), counts as counts. Field mapping only.
// ---------------------------------------------------------------------------

function adaptStarSummary(s: StarSummary): PeriodMetrics {
  return {
    pnl: s.pnl,
    sharpe: s.sharpe,
    mdd: s.mdd,
    turnover: s.turnover,
    avgPositionCount: s.minBet,
    max_weight: s.maxWeight,
    ann_ret: s.return,
    margin: s.margin,
    activeBarCount: s.nonzeroPosition,
    avgMaxBet: s.avgMaxBet,
    minPositionCount: s.minBetMin,
    quality: s.quality,
    sharpe400: s.sharpe400,
    sharpe80: s.sharpe80,
    minsoc: s.minsoc,
  };
}

export function adaptSignalPayload(source: StarSignalPayload): SignalPayload {
  return {
    heatmap: source.signalPlot,
    signalRange: source.signalRange,
    distribution: source.distribution?.counts,
  };
}

function tsArrayToRecord(timestamps: string[], values: number[]): Record<string, number | null> {
  const rec: Record<string, number | null> = {};
  for (let i = 0; i < timestamps.length; i++) {
    rec[timestamps[i]] = values[i] ?? null;
  }
  return rec;
}

// Convert cumulative PnL array → daily return fractions (diff of cumPnl).
// STAR uses additive (simple) PnL with fixed book size, so daily return =
// cumPnl[i] - cumPnl[i-1], NOT log or compounded. Values stay as raw
// fractions (no ×100) because downstream equity curve math needs them as-is.
function cumPnlToDailyReturns(timestamps: string[], cumPnl: number[], prevCumPnl: number = 0): Record<string, number | null> {
  const rec: Record<string, number | null> = {};
  for (let i = 0; i < timestamps.length; i++) {
    rec[timestamps[i]] = i === 0 ? (cumPnl[0] - prevCumPnl) : (cumPnl[i] - cumPnl[i - 1]);
  }
  return rec;
}

function buildSplitSeriesFromTimeseries(
  ts: StarTimeseries,
  isStartIndex: number,
  osStartIndex: number,
) {
  const upper = ts.timestamps.length;
  const isStart = Math.max(0, Math.min(isStartIndex, upper));
  const osStart = Math.max(isStart, Math.min(osStartIndex, upper));

  const isTs = ts.timestamps.slice(isStart, osStart);
  const osTs = ts.timestamps.slice(osStart);
  const isCumPnl = ts.cumPnl.slice(isStart, osStart);
  const osCumPnl = ts.cumPnl.slice(osStart);

  const prevIsCumPnl = isStart > 0 ? (ts.cumPnl[isStart - 1] ?? 0) : 0;
  const prevOsCumPnl = osStart > 0 ? (ts.cumPnl[osStart - 1] ?? 0) : 0;

  return {
    isRetSeries: cumPnlToDailyReturns(isTs, isCumPnl, prevIsCumPnl),
    osRetSeries: cumPnlToDailyReturns(osTs, osCumPnl, prevOsCumPnl),
    isTurnoverSeries: tsArrayToRecord(isTs, ts.turnover.slice(isStart, osStart)),
    osTurnoverSeries: tsArrayToRecord(osTs, ts.turnover.slice(osStart)),
    isMaxWeightSeries: tsArrayToRecord(isTs, ts.maxWeight.slice(isStart, osStart)),
    osMaxWeightSeries: tsArrayToRecord(osTs, ts.maxWeight.slice(osStart)),
    isNonzeroPositionSeries: tsArrayToRecord(isTs, ts.nonzeroCount.slice(isStart, osStart)),
    osNonzeroPositionSeries: tsArrayToRecord(osTs, ts.nonzeroCount.slice(osStart)),
    osStartDate: ts.timestamps[osStart],
  };
}

export function adaptSimulateResponse(resp: StarBatchResponse): BacktestRunResponse {
  if (!resp.batches || resp.batches.length === 0) {
    return {
      combined: null,
      alphas: [],
      yearly: resp.yearly,
      heatmap: null,
      signalRange: null,
      distribution: null,
    };
  }

  const single = resp.batches.length === 1;
  let combined: BacktestAlphaResult | null = null;

  if (!single) {
    const combinedSource = resp.combined ?? resp.batches[0];
    const combinedTs = combinedSource.timeseries ?? resp.timeseries;
    const combinedIsStart = combinedSource.isStartIndex ?? resp.isStartIndex;
    const combinedOsStart = combinedSource.osStartIndex ?? resp.osStartIndex;
    const combinedSeries = buildSplitSeriesFromTimeseries(combinedTs, combinedIsStart, combinedOsStart);

    combined = {
      ...adaptSignalPayload({
        signalPlot: combinedSource.signalPlot ?? resp.signalPlot,
        signalRange: combinedSource.signalRange ?? resp.signalRange,
        distribution: combinedSource.distribution ?? resp.distribution,
      }),
      name: 'combined',
      rank: 0,
      isMetrics: adaptStarSummary(combinedSource.summary.is),
      osMetrics: adaptStarSummary(combinedSource.summary.os),
      xml: combinedSource.xml,
      yearly: combinedSource.yearly ?? resp.yearly,
      longRatio: combinedSource.summary.is.longRatio,
      shortRatio: combinedSource.summary.is.shortRatio,
      ...combinedSeries,
    };
  }

  const alphas: BacktestAlphaResult[] = resp.batches.map((batch, i) => {
    const ts = batch.timeseries ?? resp.timeseries;
    const isStart = batch.isStartIndex ?? resp.isStartIndex;
    const osStart = batch.osStartIndex ?? resp.osStartIndex;
    const series = buildSplitSeriesFromTimeseries(ts, isStart, osStart);

    return {
      ...adaptSignalPayload({
        signalPlot: batch.signalPlot ?? (single ? resp.signalPlot : undefined),
        signalRange: batch.signalRange ?? (single ? resp.signalRange : undefined),
        distribution: batch.distribution ?? (single ? resp.distribution : undefined),
      }),
      name: `alpha_${batch.index}`,
      rank: i + 1,
      isMetrics: adaptStarSummary(batch.summary.is),
      osMetrics: adaptStarSummary(batch.summary.os),
      xml: batch.xml,
      yearly: batch.yearly ?? resp.yearly,
      longRatio: batch.summary.is.longRatio,
      shortRatio: batch.summary.is.shortRatio,
      ...series,
    };
  });

  return {
    combined,
    alphas,
    yearly: resp.yearly,
    // Reuse the authoritative single/combined payload; JSON contains duplicate wire objects.
    heatmap: (combined ?? alphas[0])?.heatmap ?? resp.signalPlot ?? null,
    signalRange: (combined ?? alphas[0])?.signalRange ?? resp.signalRange ?? null,
    distribution: (combined ?? alphas[0])?.distribution ?? resp.distribution?.counts ?? null,
  };
}

export function adaptGACompleteToResponse(ev: StarGACompleteEvent): BacktestRunResponse {
  return adaptSimulateResponse(ev);
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

export async function getConfig(): Promise<StarConfigResponse> {
  const response = await fetch(`${API_BASE}/api/config`);
  if (!response.ok) throw new Error(`Failed to fetch config (${response.status})`);
  return response.json();
}

export async function runSimulate(
  xml: string,
  simStart: string,
  isStart: string,
  osStart: string,
  signal?: AbortSignal,
  threads?: number,
  mode: 0 | 1 = 1,
): Promise<StarBatchResponse> {
  const response = await fetch(`${API_BASE}/api/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, simStart, isStart, osStart, threads, mode, collectSignals: true, collectSignalsTopK: 5 }),
    signal,
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data = await response.json();
      detail = parseApiErrorPayload(data);
    } catch {
      // ignore json parse errors
    }

    const message = detail || response.statusText || `HTTP ${response.status}`;
    throw new Error(`Simulate failed (${response.status}): ${message}`);
  }

  return response.json();
}

export async function runGAStream(
  xml: string,
  simStart: string,
  isStart: string,
  osStart: string,
  popSize: number,
  generations: number,
  threads: number,
  onEvent: (event: StarGAEvent) => void,
  signal?: AbortSignal,
  mode: 0 | 1 = 1,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/ga/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, simStart, isStart, osStart, popSize, generations, threads, mode, collectSignals: true }),
    signal,
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data = await response.json();
      detail = parseApiErrorPayload(data);
    } catch { /* ignore */ }
    throw new Error(detail || `GA failed (${response.status})`);
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (line.trim()) {
        onEvent(JSON.parse(line));
      }
    }
  }
  if (buffer.trim()) {
    onEvent(JSON.parse(buffer));
  }
}

// ---------------------------------------------------------------------------
// Alpha CRUD
// ---------------------------------------------------------------------------

export async function saveAlpha(name: string, xml: string): Promise<{ success: boolean; name: string }> {
  const response = await fetch(`${API_BASE}/api/alphas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, xml }),
  });

  if (!response.ok) {
    const message = response.statusText || `HTTP ${response.status}`;
    throw new Error(`Failed to save alpha: ${message}`);
  }

  const data = await response.json();
  return { success: data.saved, name: data.name };
}

export async function getAlphaList(): Promise<AlphaListResponse> {
  const response = await fetch(`${API_BASE}/api/alphas`);
  if (!response.ok) throw new Error('Failed to fetch alpha list');
  const data = await response.json();
  return { alphas: data };
}

export async function getAlpha(name: string): Promise<AlphaDetailResponse> {
  // C++ server list endpoint returns [{name, xml}, ...] — no per-item detail endpoint
  const response = await fetch(`${API_BASE}/api/alphas`);
  if (!response.ok) throw new Error('Failed to fetch alpha list');
  const data: Array<{ name: string; xml: string }> = await response.json();
  const found = data.find((a) => a.name === name);
  if (!found) throw new Error(`Alpha not found: ${name}`);
  return { name: found.name, xml: found.xml };
}

export async function deleteAlpha(name: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/alphas/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  if (!response.ok) throw new Error(`Failed to delete alpha: ${response.statusText || `HTTP ${response.status}`}`);
}
