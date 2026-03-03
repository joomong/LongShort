import type {
  StarConfigResponse,
  StarSimulateResponse,
  StarBatchResponse,
  StarGAEvent,
  StarGACompleteEvent,
  StarSummary,
  StarTimeseries,
  BacktestRunResponse,
  BacktestAlphaResult,
  BacktestCombinedResult,
  ISMetrics,
  OSMetrics,
  AlphaListResponse,
  AlphaDetailResponse,
} from '@/types';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

// ---------------------------------------------------------------------------
// Adapter helpers
// C++ STAR engine returns fractions (0.874 = 87.4%). Frontend displays
// percentage points, so pnl/ann_ret/mdd/turnover/min_bet are ×100 here.
// max_weight stays as fraction — the display layer does ×100 itself.
// ---------------------------------------------------------------------------

function adaptStarToIS(s: StarSummary): ISMetrics {
  return {
    pnl: s.pnl * 100,
    sharpe: s.sharpe,
    mdd: s.mdd * 100,
    turnover: s.turnover * 100,
    min_bet: s.minBet * 100,
    max_weight: s.maxWeight,
    ann_ret: s.return * 100,
    margin: s.margin,
    nonzeroPosition: s.nonzeroPosition,
    avgMaxBet: s.avgMaxBet,
    minBetMin: s.minBetMin * 100,
    quality: s.quality,
    sharpe400: s.sharpe400,
    sharpe80: s.sharpe80,
    minsoc: s.minsoc,
  };
}

function adaptStarToOS(s: StarSummary): OSMetrics {
  return {
    pnl: s.pnl * 100,
    sharpe: s.sharpe,
    mdd: s.mdd * 100,
    ann_ret: s.return * 100,
    turnover: s.turnover * 100,
    min_bet: s.minBet * 100,
    max_weight: s.maxWeight,
    margin: s.margin,
    nonzeroPosition: s.nonzeroPosition,
    avgMaxBet: s.avgMaxBet,
    minBetMin: s.minBetMin * 100,
    quality: s.quality,
    sharpe400: s.sharpe400,
    sharpe80: s.sharpe80,
    minsoc: s.minsoc,
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

export function adaptSingleResponse(resp: StarSimulateResponse): BacktestRunResponse {
  const isMetrics = adaptStarToIS(resp.summary.is);
  const osMetrics = adaptStarToOS(resp.summary.os);

  const ts: StarTimeseries = resp.timeseries;

  const isTs = ts.timestamps.slice(resp.isStartIndex, resp.osStartIndex);
  const osTs = ts.timestamps.slice(resp.osStartIndex);
  const isCumPnl = ts.cumPnl.slice(resp.isStartIndex, resp.osStartIndex);
  const osCumPnl = ts.cumPnl.slice(resp.osStartIndex);

  const alpha: BacktestAlphaResult = {
    name: 'alpha_0',
    rank: 1,
    isMetrics,
    osMetrics,
    yearly: resp.yearly,
    isRetSeries: cumPnlToDailyReturns(isTs, isCumPnl, resp.isStartIndex > 0 ? ts.cumPnl[resp.isStartIndex - 1] : 0),
    osRetSeries: cumPnlToDailyReturns(osTs, osCumPnl, ts.cumPnl[resp.osStartIndex - 1]),
    isTurnoverSeries: tsArrayToRecord(isTs, ts.turnover.slice(resp.isStartIndex, resp.osStartIndex)),
    osTurnoverSeries: tsArrayToRecord(osTs, ts.turnover.slice(resp.osStartIndex)),
    isMaxWeightSeries: tsArrayToRecord(isTs, ts.maxWeight.slice(resp.isStartIndex, resp.osStartIndex)),
    osMaxWeightSeries: tsArrayToRecord(osTs, ts.maxWeight.slice(resp.osStartIndex)),
    isNonzeroPositionSeries: tsArrayToRecord(isTs, ts.nonzeroCount.slice(resp.isStartIndex, resp.osStartIndex)),
    osNonzeroPositionSeries: tsArrayToRecord(osTs, ts.nonzeroCount.slice(resp.osStartIndex)),
    isEquitySeries: tsArrayToRecord(isTs, isCumPnl),
    osEquitySeries: tsArrayToRecord(osTs, osCumPnl),
    osStartDate: ts.timestamps[resp.osStartIndex],
    longRatio: resp.summary.is.longRatio,
    shortRatio: resp.summary.is.shortRatio,
  };

  return {
    combined: { isMetrics, osMetrics, isRetSeries: alpha.isRetSeries, osRetSeries: alpha.osRetSeries },
    alphas: [alpha],
    yearly: resp.yearly,
    heatmap: resp.signalPlot ?? null,
    signalRange: resp.signalRange ?? null,
    distribution: resp.distribution ? resp.distribution.counts : null,
  };
}

export function adaptBatchResponse(resp: StarBatchResponse): BacktestRunResponse {
  const ts: StarTimeseries = resp.timeseries;

  const alphas: BacktestAlphaResult[] = resp.batches.map((batch, i) => ({
    name: `alpha_${batch.index}`,
    rank: i + 1,
    isMetrics: adaptStarToIS(batch.summary.is),
    osMetrics: adaptStarToOS(batch.summary.os),
    longRatio: batch.summary.is.longRatio,
    shortRatio: batch.summary.is.shortRatio,
  }));

  const best = resp.batches[0];
  const combined: BacktestCombinedResult = {
    isMetrics: adaptStarToIS(best.summary.is),
    osMetrics: adaptStarToOS(best.summary.os),
    isRetSeries: cumPnlToDailyReturns(
      ts.timestamps.slice(resp.isStartIndex, resp.osStartIndex),
      ts.cumPnl.slice(resp.isStartIndex, resp.osStartIndex),
      resp.isStartIndex > 0 ? ts.cumPnl[resp.isStartIndex - 1] : 0,
    ),
    osRetSeries: cumPnlToDailyReturns(
      ts.timestamps.slice(resp.osStartIndex),
      ts.cumPnl.slice(resp.osStartIndex),
      ts.cumPnl[resp.osStartIndex - 1],
    ),
  };

  return {
    combined,
    alphas,
    yearly: resp.yearly,
    heatmap: null,
    signalRange: null,
    distribution: null,
  };
}

export function adaptGACompleteToResponse(ev: StarGACompleteEvent): BacktestRunResponse {
  const isMetrics = adaptStarToIS(ev.summary.is);
  const osMetrics = adaptStarToOS(ev.summary.os);
  const ts: StarTimeseries = ev.timeseries;

  const alpha: BacktestAlphaResult = {
    name: 'ga_best',
    rank: 1,
    isMetrics,
    osMetrics,
    yearly: ev.yearly,
    xml: ev.bestXml,
    isRetSeries: cumPnlToDailyReturns(
      ts.timestamps.slice(ev.isStartIndex, ev.osStartIndex),
      ts.cumPnl.slice(ev.isStartIndex, ev.osStartIndex),
      ev.isStartIndex > 0 ? ts.cumPnl[ev.isStartIndex - 1] : 0,
    ),
    osRetSeries: cumPnlToDailyReturns(
      ts.timestamps.slice(ev.osStartIndex),
      ts.cumPnl.slice(ev.osStartIndex),
      ts.cumPnl[ev.osStartIndex - 1],
    ),
    isTurnoverSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.isStartIndex, ev.osStartIndex),
      ts.turnover.slice(ev.isStartIndex, ev.osStartIndex)
    ),
    osTurnoverSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.osStartIndex),
      ts.turnover.slice(ev.osStartIndex)
    ),
    isMaxWeightSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.isStartIndex, ev.osStartIndex),
      ts.maxWeight.slice(ev.isStartIndex, ev.osStartIndex)
    ),
    osMaxWeightSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.osStartIndex),
      ts.maxWeight.slice(ev.osStartIndex)
    ),
    isNonzeroPositionSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.isStartIndex, ev.osStartIndex),
      ts.nonzeroCount.slice(ev.isStartIndex, ev.osStartIndex)
    ),
    osNonzeroPositionSeries: tsArrayToRecord(
      ts.timestamps.slice(ev.osStartIndex),
      ts.nonzeroCount.slice(ev.osStartIndex)
    ),
    isEquitySeries: tsArrayToRecord(
      ts.timestamps.slice(ev.isStartIndex, ev.osStartIndex),
      ts.cumPnl.slice(ev.isStartIndex, ev.osStartIndex)
    ),
    osEquitySeries: tsArrayToRecord(
      ts.timestamps.slice(ev.osStartIndex),
      ts.cumPnl.slice(ev.osStartIndex)
    ),
    osStartDate: ts.timestamps[ev.osStartIndex],
    longRatio: ev.summary.is.longRatio,
    shortRatio: ev.summary.is.shortRatio,
  };

  return {
    combined: { isMetrics, osMetrics, isRetSeries: alpha.isRetSeries, osRetSeries: alpha.osRetSeries },
    alphas: [alpha],
    yearly: ev.yearly,
    heatmap: null,
    signalRange: null,
    distribution: null,
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
  isStart: string,
  osStart: string
): Promise<StarSimulateResponse | StarBatchResponse> {
  const response = await fetch(`${API_BASE}/api/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, isStart, osStart }),
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data = await response.json();
      if (typeof data?.detail === 'string') {
        detail = data.detail;
      } else if (Array.isArray(data?.detail)) {
        detail = data.detail
          .map((d: unknown) => {
            if (d && typeof d === 'object' && 'msg' in d) {
              const msg = (d as Record<string, unknown>).msg;
              if (typeof msg === 'string') return msg;
            }
            return JSON.stringify(d);
          })
          .join('; ');
      }
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
  isStart: string,
  osStart: string,
  popSize: number,
  generations: number,
  threads: number,
  onEvent: (event: StarGAEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/ga/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ xml, isStart, osStart, popSize, generations, threads }),
    signal,
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data = await response.json();
      detail = typeof data?.detail === 'string' ? data.detail : undefined;
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
