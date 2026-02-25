// Types aligned to the current backend API contract.
// Source of truth:
// - backend: api/models/schemas.py
// - backtest responses: api/services/backtest_service.py + framework/backtester.py

export interface HeatmapData {
  timestamps: string[];
  symbols: string[];
  values: (number | null)[][]; // [time][symbol]
}

export interface SignalRange {
  min: number;
  max: number;
}

export interface ISMetrics {
  // backend returns a superset depending on path; keep fields optional-friendly
  pnl: number;
  sharpe: number;
  mdd?: number;
  turnover?: number;
  cagr?: number;
  min_bet?: number;
  max_weight?: number;
  ir?: number;
  corr?: number;
}

export interface OSMetrics {
  pnl: number;
  cagr?: number;
  sharpe: number;
  mdd?: number;
  ir?: number;
  corr?: number;
}

export interface BacktestAlphaResult {
  name: string;
  rank: number;
  isMetrics: ISMetrics;
  osMetrics: OSMetrics;

  // Optional: the fully-expanded per-alpha XML pipeline (used for detail view)
  xml?: string;

  // present in multi-alpha path
  isRetSeries?: Record<string, number | null>;
  osRetSeries?: Record<string, number | null>;

  // detail view time series
  isTurnoverSeries?: Record<string, number | null> | null;
  osTurnoverSeries?: Record<string, number | null> | null;
  isMinBetSeries?: Record<string, number | null> | null;
  osMinBetSeries?: Record<string, number | null> | null;
  isMaxWeightSeries?: Record<string, number | null> | null;
  osMaxWeightSeries?: Record<string, number | null> | null;
  isNonzeroPositionSeries?: Record<string, number | null> | null;
  osNonzeroPositionSeries?: Record<string, number | null> | null;
  isEquitySeries?: Record<string, number | null> | null;
  osEquitySeries?: Record<string, number | null> | null;
  longRatio?: number;
  shortRatio?: number;
}

export interface BacktestCombinedResult {
  isMetrics: ISMetrics;
  osMetrics: OSMetrics;
  isRetSeries?: Record<string, number | null>;
  osRetSeries?: Record<string, number | null>;
}

export interface BacktestRunResponse {
  combined: BacktestCombinedResult | null;
  alphas: BacktestAlphaResult[];
  initialCapital?: number;
  sortedBy?: string;

  // single-alpha path returns viz bundle
  heatmap?: HeatmapData | null;
  signalRange?: SignalRange | null;
  distribution?: number[] | null;
}

export interface BacktestSettings {
  osStart: string;
  osEnd: string;
  isStart: string;
  isEnd: string;
  // D0: 당일, D1: 익일, intraday: 장중 고정시간
  barInterval: 'D0' | 'D1' | 'intraday';
  nJobs: number;
  gaPopulation: number;
}

// Alpha CRUD

export interface AlphaInfo {
  name: string;
  createdAt: string;
}

export interface AlphaListResponse {
  alphas: AlphaInfo[];
}

export interface AlphaDetailResponse {
  name: string;
  xml: string;
  createdAt: string;
}

// Snapshot

export interface PinnedSnapshot {
  id: string;
  name: string;
  color: string;
  retSeries: Record<string, number>; // date → value
}

// UI

export interface GAProgress {
  generation: number;
  totalGenerations: number;
  evaluated: number;
  populationSize: number;
}

export type ViewMode = 'single' | 'multi' | 'detail';
