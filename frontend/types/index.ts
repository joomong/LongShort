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
  /** Daily gross-return mean / sample standard deviation, without annualization. */
  ir?: number | null;
  /** Sharpe * sqrt(abs(annRet / 100) / max(dailyTvr, 0.125)), on the full gross book. */
  fitness?: number | null;
  winLose?: number | null; // positive-return bars / valid return bars, percentage points
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
  linear?: StarDistribution; // full-range companion with the same observation population
  scale?: 'log' | 'negative-log' | 'symlog' | 'linear';
  linthresh?: number;
  totalCount?: number;
  exampleValue?: (number | null)[];
  edges?: number[];
  rangeLo?: number; // inner histogram range; outer bins retain the tails
  rangeHi?: number;
  bins: number[];
  counts: number[];
  min: number;
  max: number;
  exampleSymbol?: (string | null)[]; // per bin: the cell with the largest |value|
  exampleDate?: (string | null)[];
}

export interface StarSignalPayload {
  signalPlot?: HeatmapData;
  signalRange?: SignalRange;
  distribution?: StarDistribution;
  signalDistributionLog?: StarDistribution;
}

/** Correlation of a run's daily net return with the alpha pool (engine `alpha_pool`), over the sim window. */
export interface PoolCorrelation {
  /** Equal-weight pool (alphas active on each bar); null with fewer than 60 overlapping days. */
  ew: number | null;
  days: number;
}

export interface StarBatchEntry extends StarSignalPayload {
  index: number;
  poolCorr?: PoolCorrelation;
  xml?: string;
  summary: { is: StarSummary; os: StarSummary };
  yearly?: StarYearly[];
  timeseries?: StarTimeseries;
  isStartIndex?: number;
  osStartIndex?: number;
}

export interface StarBatchResponse extends StarSignalPayload {
  simStart?: string;
  simEnd?: string;
  executionPrice?: 'close' | 'ohlc4' | 'vwap';
  batches: StarBatchEntry[];
  yearly: StarYearly[];
  timeseries: StarTimeseries;
  isStartIndex: number;
  osStartIndex: number;
  combined?: StarBatchEntry;
}

export interface StarConfigResponse {
  symbols: string[];
  alphaPool?: string | null;
  alphaPoolSize?: number;
  alphaPoolUniverse?: string | null;
  symbolLabels?: Record<string, string>; // panel symbol -> ticker
  simStart: string;
  simEnd: string;
  feeRate: number;
  barsPerYear: number;
  executionPrice?: 'close' | 'ohlc4' | 'vwap';
  supportsSimEnd?: boolean;
  executionPrices?: Array<'close' | 'ohlc4' | 'vwap'>;
  universes?: string[];      // selectable universe names; empty = a single configured universe
  defaultUniverse?: string;
  alphasDir?: string;         // absolute alpha save directory
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
  simStart?: string;
  simEnd?: string;
  executionPrice?: 'close' | 'ohlc4' | 'vwap';
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

export interface StarGACandidate extends Partial<StarBatchEntry> {
  index: number;
  xml: string;
  round: number;
  origin: string;
  status: 'completed' | 'failed';
  error?: string;
}

export type StarGASearchEvent =
  | { type: 'search_start'; simStart: string; simEnd: string; executionPrice: 'close' | 'ohlc4' | 'vwap'; universe: string }
  | { type: 'round_start'; round: number; size: number }
  | { type: 'candidate'; candidate: StarGACandidate }
  | { type: 'round_end'; round: number; bestSharpe: number | null }
  | { type: 'search_complete'; stopReason: string; evaluated: number; bestXml: string };

export type StarGAEvent = StarGAGenerationEvent | StarGACompleteEvent | StarGAErrorEvent | StarGASearchEvent;

// ---------------------------------------------------------------------------
// Frontend display types (backward-compatible with existing components)
// ---------------------------------------------------------------------------

export interface HeatmapData {
  timestamps: string[];
  symbols: string[];
  values: (number | null)[][]; // [time][symbol]
  /** Per returned row, '1'/'0' per returned symbol: in the run's universe on that signal date. */
  membership?: string[];
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
  distributionBins?: number[] | null;
  distributionRange?: SignalRange | null;
  signalDistributionLog?: StarDistribution | null;
  distributionExamples?: { symbol: (string | null)[]; date: (string | null)[] } | null;
}

export interface BacktestAlphaResult extends SignalPayload {
  candidateId?: number;
  round?: number;
  error?: string;
  detailLoaded?: boolean;
  detailError?: string;
  name: string;
  rank: number;
  isMetrics: ISMetrics;
  osMetrics: OSMetrics;
  xml?: string;
  yearly?: StarYearly[];

  isRetSeries?: Record<string, number | null>;
  osRetSeries?: Record<string, number | null>;
  // Gross (pre-fee) daily returns from the engine's cumPnlGross.
  isGrossRetSeries?: Record<string, number | null>;
  osGrossRetSeries?: Record<string, number | null>;

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
  poolCorr?: PoolCorrelation;
}

export interface BacktestRunResponse extends SignalPayload {
  runSettings?: Pick<BacktestSettings, 'simStart' | 'isStart' | 'osStart'> & { simEnd?: string; threads: number; universe?: string; executionPrice?: 'close' | 'ohlc4' | 'vwap' };
  /** Editor XML verbatim that started this run (or the saved alpha.xml for a loaded result). */
  sourceXml?: string;
  /** Saved alpha name when rows/detail/batch are read from saved files instead of the engine. */
  savedSource?: string;
  combined: BacktestAlphaResult | null;
  alphas: BacktestAlphaResult[];
  yearly?: StarYearly[];
  initialCapital?: number;
  sortedBy?: string;
}

export interface BacktestSettings {
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  executionPrice: 'close' | 'ohlc4' | 'vwap';
  universe: string; // '' = engine default
  nJobs: number;
  gaPopulation: number;
  gaFirstSearch?: number;
  gaStopRound?: number;
  gaStopAlphas?: number;
}

export interface GARound {
  round: number;
  status: 'running' | 'completed' | 'stopped';
}

// Alpha CRUD

export type SavedAlphaLevel = 'xml' | 'results' | 'positions';

/** Toolbar run settings stored with a saved alpha (metadata.yml). */
export interface SavedAlphaMetadata {
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  universe: string;
  executionPrice: 'close' | 'ohlc4' | 'vwap';
}

export interface SavedAlphaRow {
  id: number;
  round: number;
  xml: string;
}

export interface SaveAlphaRequest {
  name: string;
  xml: string;
  level: SavedAlphaLevel;
  metadata: SavedAlphaMetadata;
  rows?: SavedAlphaRow[];
}

export interface SavedAlphaInfo {
  name: string;
  xml: string;
  level: SavedAlphaLevel;
  metadata: SavedAlphaMetadata;
}

export interface SavedAlphaResults {
  rows: StarGACandidate[];
  hasBatch: boolean;
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
