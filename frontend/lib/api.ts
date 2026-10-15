import type {
  StarConfigResponse,
  StarBatchResponse,
  StarGAEvent,
  StarGACompleteEvent,
  StarGACandidate,
  StarSummary,
  StarTimeseries,
  BacktestRunResponse,
  BacktestAlphaResult,
  PeriodMetrics,
  StarSignalPayload,
  SignalPayload,
  SaveAlphaRequest,
  SavedAlphaInfo,
  SavedAlphaResults,
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
    signalDistributionLog: source.signalDistributionLog ?? null,
    distributionBins: source.distribution?.bins,
    distributionRange: source.distribution ? {
      min: source.distribution.rangeLo ?? source.distribution.min,
      max: source.distribution.rangeHi ?? source.distribution.max,
    } : null,
    distributionExamples: source.distribution?.exampleSymbol && source.distribution?.exampleDate
      ? { symbol: source.distribution.exampleSymbol, date: source.distribution.exampleDate } : null,
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
  const gross = ts.cumPnlGross;
  const hasGross = Array.isArray(gross) && gross.length === ts.timestamps.length;

  return {
    isRetSeries: cumPnlToDailyReturns(isTs, isCumPnl, prevIsCumPnl),
    osRetSeries: cumPnlToDailyReturns(osTs, osCumPnl, prevOsCumPnl),
    ...(hasGross ? {
      isGrossRetSeries: cumPnlToDailyReturns(
        isTs, gross.slice(isStart, osStart), isStart > 0 ? (gross[isStart - 1] ?? 0) : 0),
      osGrossRetSeries: cumPnlToDailyReturns(
        osTs, gross.slice(osStart), osStart > 0 ? (gross[osStart - 1] ?? 0) : 0),
    } : {}),
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
        signalDistributionLog: combinedSource.signalDistributionLog ?? resp.signalDistributionLog,
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
        signalDistributionLog: batch.signalDistributionLog ?? (single ? resp.signalDistributionLog : undefined),
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
    signalDistributionLog: (combined ?? alphas[0])?.signalDistributionLog ?? resp.signalDistributionLog ?? null,
  };
}

export function adaptGACompleteToResponse(ev: StarGACompleteEvent): BacktestRunResponse {
  return adaptSimulateResponse(ev);
}

export function adaptGACandidate(candidate: StarGACandidate): BacktestAlphaResult {
  const blank: PeriodMetrics = { pnl: NaN, sharpe: NaN, mdd: NaN, turnover: NaN, avgPositionCount: NaN, max_weight: NaN };
  return {
    candidateId: candidate.index,
    round: candidate.round,
    name: `alpha_${candidate.index}`,
    rank: candidate.index + 1,
    xml: candidate.xml,
    error: candidate.error,
    isMetrics: candidate.summary ? adaptStarSummary(candidate.summary.is) : blank,
    osMetrics: candidate.summary ? adaptStarSummary(candidate.summary.os) : blank,
    yearly: candidate.yearly,
    detailLoaded: false,
  };
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
  extra: { simEnd?: string; signalPlotSymbols?: string[]; universe?: string; executionPrice?: 'close' | 'ohlc4' | 'vwap'; metricsOnly?: boolean; signalSeriesOnly?: boolean; xmls?: string[]; combinedOnly?: boolean } = {},
): Promise<StarBatchResponse> {
  const response = await fetch(`${API_BASE}/api/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, simStart, isStart, osStart, threads, collectSignals: true, collectSignalsTopK: 5, ...extra, universe: extra.universe || undefined }),
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

export async function runCombinedAlphas(
  xmls: string[], settings: NonNullable<BacktestRunResponse['runSettings']>, signal?: AbortSignal,
): Promise<BacktestAlphaResult> {
  const response = await runSimulate('', settings.simStart, settings.isStart, settings.osStart, signal, settings.threads,
    { xmls, combinedOnly: true, metricsOnly: true, simEnd: settings.simEnd, universe: settings.universe, executionPrice: settings.executionPrice });
  if (!response.combined) throw new Error('No combined performance returned.');
  const detail = adaptSimulateResponse({ ...response, batches: [response.combined] }).alphas[0];
  return { ...detail, name: 'combined', rank: 0, detailLoaded: true };
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
  universe?: string,
  executionPrice?: 'close' | 'ohlc4' | 'vwap',
  simEnd?: string,
  search: { stopAlphas?: number; roundSize?: number } = {},
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/ga/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, simStart, isStart, osStart, firstSearch: popSize, stopRound: generations, stopAlphas: search.stopAlphas ?? 400, roundSize: search.roundSize ?? 50, threads, streamCandidates: true, universe: universe || undefined, executionPrice, simEnd: simEnd || undefined }),
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
  let completed = false;
  const receive = (line: string) => {
    const event: StarGAEvent = JSON.parse(line);
    if (event.type === 'complete' || event.type === 'search_complete' || event.type === 'error') completed = true;
    onEvent(event);
  };

  // A parse or handler error must drop the connection: the engine stops the GA only on disconnect.
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (line.trim()) {
          receive(line);
        }
      }
    }
    if (buffer.trim()) {
      receive(buffer);
    }
  } finally {
    reader.cancel().catch(() => {});
  }
  if (!completed) throw new Error('GA connection ended before completion.');
}

// ---------------------------------------------------------------------------
// Alpha CRUD
// ---------------------------------------------------------------------------

export class AlphaExistsError extends Error {}

async function alphaError(response: Response, action: string): Promise<Error> {
  let detail: string | undefined;
  try { detail = parseApiErrorPayload(await response.json()); } catch { /* ignore */ }
  return new Error(`${action} (${response.status}): ${detail || response.statusText || `HTTP ${response.status}`}`);
}

export async function saveAlpha(request: SaveAlphaRequest): Promise<{ name: string; level: string; path?: string }> {
  const response = await fetch(`${API_BASE}/api/alphas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  if (response.status === 409) throw new AlphaExistsError(`An alpha named "${request.name}" already exists.`);
  if (!response.ok) throw await alphaError(response, 'Failed to save alpha');
  return response.json();
}

export async function getAlphaList(): Promise<SavedAlphaInfo[]> {
  const response = await fetch(`${API_BASE}/api/alphas`);
  if (!response.ok) throw new Error('Failed to fetch alpha list');
  return response.json();
}

const alphaPath = (name: string) => `${API_BASE}/api/alphas/${encodeURIComponent(name)}`;

export async function getSavedResults(name: string): Promise<SavedAlphaResults> {
  const response = await fetch(`${alphaPath(name)}/results`);
  if (!response.ok) throw await alphaError(response, 'Failed to load saved results');
  return response.json();
}

export async function getSavedRow(name: string, id: number, signal?: AbortSignal): Promise<StarBatchResponse> {
  const response = await fetch(`${alphaPath(name)}/results/rows/${id}`, { signal });
  if (!response.ok) throw await alphaError(response, 'Failed to load saved row');
  return response.json();
}

export async function getSavedBatch(name: string, signal?: AbortSignal): Promise<BacktestAlphaResult> {
  const response = await fetch(`${alphaPath(name)}/results/batch`, { signal });
  if (!response.ok) throw await alphaError(response, 'Failed to load saved batch');
  const body: StarBatchResponse = await response.json();
  if (!body.combined) throw new Error('No combined performance saved.');
  const detail = adaptSimulateResponse({ ...body, batches: [body.combined] }).alphas[0];
  return { ...detail, name: 'combined', rank: 0, detailLoaded: true };
}

export async function deleteAlpha(name: string): Promise<void> {
  const response = await fetch(alphaPath(name), { method: 'DELETE' });
  if (!response.ok) throw await alphaError(response, 'Failed to delete alpha');
}
