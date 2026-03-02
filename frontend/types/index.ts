// Types for C++ STAR API + backward-compatible frontend types.
// Source of truth: cpp-engine/src/api/json_serializer.h

// ---------------------------------------------------------------------------
// C++ STAR API response types (raw from server)
// ---------------------------------------------------------------------------

export interface StarSummary {
  return: number;
  sharpe: number;
  mdd: number;
  turnover: number;
  maxWeight: number;
  minBet: number;
  pnl: number;
  winRate: number;
  margin: number;
  longRatio: number;
  shortRatio: number;
  nonzeroPosition: number;
}

export interface StarTimeseries {
  timestamps: string[];
  cumPnl: number[];
  cumPnlGross: number[];
  turnover: number[];
  maxWeight: number[];
  nonzeroCount: number[];
  drawdown: number[];
}

export interface StarYearly {
  year: number;
  longSM: number;
  shortSM: number;
  pnl: number;
  annRet: number;
  sharpe: number;
  maxDD: number;
  dailyTvr: number;
  margin: number;
}

export interface StarDistribution {
  bins: number[];
  counts: number[];
  min: number;
  max: number;
}

export interface StarSimulateResponse {
  summary: { is: StarSummary; os: StarSummary };
  yearly: StarYearly[];
  timeseries: StarTimeseries;
  isStartIndex: number;
  osStartIndex: number;
  signalPlot?: HeatmapData;
  signalRange?: SignalRange;
  distribution?: StarDistribution;
}

export interface StarBatchEntry {
  index: number;
  summary: { is: StarSummary; os: StarSummary };
}

export interface StarBatchResponse {
  batches: StarBatchEntry[];
  yearly: StarYearly[];
  timeseries: StarTimeseries;
  isStartIndex: number;
  osStartIndex: number;
}

export interface StarConfigResponse {
  symbols: string[];
  simStart: string;
  simEnd: string;
  feeRate: number;
  barsPerYear: number;
  T: number;
  N: number;
  columns: string[];
}

export interface StarGAGenerationEvent {
  type: 'generation_end';
  generation: number;
  totalGenerations: number;
  bestSharpe: number;
  bestXml: string;
}

export interface StarGACompleteEvent {
  type: 'complete';
  bestXml: string;
  summary: { is: StarSummary; os: StarSummary };
  yearly: StarYearly[];
  timeseries: StarTimeseries;
  isStartIndex: number;
  osStartIndex: number;
}

export interface StarGAErrorEvent {
  type: 'error';
  error: string;
}

export type StarGAEvent = StarGAGenerationEvent | StarGACompleteEvent | StarGAErrorEvent;

// ---------------------------------------------------------------------------
// Frontend display types (backward-compatible with existing components)
// ---------------------------------------------------------------------------

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
  pnl: number;
  sharpe: number;
  mdd?: number;
  /** Percentage points, e.g. 26.7 means 26.7% daily turnover */
  turnover?: number;
  ann_ret?: number;
  /** Percentage points, e.g. 0.35 means 0.35 active symbols */
  min_bet?: number;
  /** Raw fraction, e.g. 0.6 means 60% — display layer multiplies by 100 */
  max_weight?: number;
  margin?: number;
  nonzeroPosition?: number;
  ir?: number;
  corr?: number;
}

export interface OSMetrics {
  pnl: number;
  ann_ret?: number;
  sharpe: number;
  mdd?: number;
  turnover?: number;
  min_bet?: number;
  max_weight?: number;
  margin?: number;
  nonzeroPosition?: number;
  ir?: number;
  corr?: number;
}

export interface BacktestAlphaResult {
  name: string;
  rank: number;
  isMetrics: ISMetrics;
  osMetrics: OSMetrics;
  xml?: string;
  yearly?: StarYearly[];

  isRetSeries?: Record<string, number | null>;
  osRetSeries?: Record<string, number | null>;

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
  osStartDate?: string;
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
  yearly?: StarYearly[];
  initialCapital?: number;
  sortedBy?: string;
  heatmap?: HeatmapData | null;
  signalRange?: SignalRange | null;
  distribution?: number[] | null;
}

export interface BacktestSettings {
  isStart: string;
  osStart: string;
  barInterval: 'D0' | 'D1' | 'intraday';
  nJobs: number;
  gaPopulation: number;
}

// Alpha CRUD

export interface AlphaInfo {
  name: string;
  createdAt?: string;
}

export interface AlphaListResponse {
  alphas: AlphaInfo[];
}

export interface AlphaDetailResponse {
  name: string;
  xml: string;
  createdAt?: string;
}

// Snapshot

export interface PinnedSnapshot {
  id: string;
  name: string;
  color: string;
  retSeries: Record<string, number>;
}

// UI

export interface GAProgress {
  generation: number;
  totalGenerations: number;
  evaluated: number;
  populationSize: number;
  bestSharpe?: number;
}

export type ViewMode = 'single' | 'multi' | 'detail';
