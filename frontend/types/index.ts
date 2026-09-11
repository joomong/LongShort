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
  /** Legacy wire key: average number of held symbols per bar. */
  minBet: number;
  pnl: number;
  winRate: number;
  margin: number;
  longRatio: number;
  shortRatio: number;
  /** Legacy wire key: number of bars with at least one held symbol. */
  nonzeroPosition: number;
  avgMaxBet: number;
  /** Legacy wire key: minimum number of held symbols in the period. */
  minBetMin: number;
  quality: number;
  sharpe400: number;
  sharpe80: number;
  minsoc: number;
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

export interface StarSignalPayload {
  signalPlot?: HeatmapData;
  signalRange?: SignalRange;
  distribution?: StarDistribution;
}

export interface StarBatchEntry extends StarSignalPayload {
  index: number;
  xml?: string;
  summary: { is: StarSummary; os: StarSummary };
  yearly?: StarYearly[];
  timeseries?: StarTimeseries;
  isStartIndex?: number;
  osStartIndex?: number;
}

export interface StarBatchResponse extends StarSignalPayload {
  mode?: 0 | 1;
  batches: StarBatchEntry[];
  yearly: StarYearly[];
  timeseries: StarTimeseries;
  isStartIndex: number;
  osStartIndex: number;
  combined?: StarBatchEntry;
}

export interface StarConfigResponse {
  symbols: string[];
  simStart: string;
  simEnd: string;
  feeRate: number;
  barsPerYear: number;
  mode?: 0 | 1;
  T: number;
  N: number;
  columns: string[];
  loadedFields?: number;
  loadedDataBytes?: number;
}

export interface StarGAGenerationEvent {
  type: 'generation_end';
  generation: number;
  totalGenerations: number;
  bestSharpe: number;
  bestXml: string;
}

export interface StarGACompleteEvent extends StarSignalPayload {
  mode?: 0 | 1;
  type: 'complete';
  bestXml: string;
  batches: StarBatchEntry[];
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
  rowStride?: number;
  symbolStride?: number;
  originalRows?: number;
  originalSymbols?: number;
  sampled?: boolean;
}

export interface SignalRange {
  min: number;
  max: number;
}

export interface PeriodMetrics {
  pnl: number;
  sharpe: number;
  mdd?: number;
  /** Percentage points, e.g. 26.7 means 26.7% daily turnover */
  turnover?: number;
  ann_ret?: number;
  /** Average number of held symbols per bar (UI avgBet). */
  avgPositionCount?: number;
  /** Percentage points, e.g. 60.0 means 60% */
  max_weight?: number;
  margin?: number;
  /** Number of bars with at least one held symbol. */
  activeBarCount?: number;
  avgMaxBet?: number;
  /** Minimum number of held symbols in the period (UI minBet). */
  minPositionCount?: number;
  quality?: number;
  sharpe400?: number;
  sharpe80?: number;
  minsoc?: number;
  ir?: number;
  corr?: number;
}

export type ISMetrics = PeriodMetrics;
export type OSMetrics = PeriodMetrics;

export interface SignalPayload {
  heatmap?: HeatmapData | null;
  signalRange?: SignalRange | null;
  distribution?: number[] | null;
}

export interface BacktestAlphaResult extends SignalPayload {
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
  osStartDate?: string;
  longRatio?: number;
  shortRatio?: number;
}

export interface BacktestRunResponse extends SignalPayload {
  runSettings?: Pick<BacktestSettings, 'simStart' | 'isStart' | 'osStart'> & { mode: 0 | 1; threads: number };
  combined: BacktestAlphaResult | null;
  alphas: BacktestAlphaResult[];
  yearly?: StarYearly[];
  initialCapital?: number;
  sortedBy?: string;
}

export interface BacktestSettings {
  simStart: string;
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
