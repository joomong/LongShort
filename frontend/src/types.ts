export type ExecutionPrice = 'close' | 'ohlc4' | 'vwap';

export interface RunSettings {
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  executionPrice: ExecutionPrice;
  universe: string;
  threads: number;
  firstSearch: number;
  roundSize: number;
  stopRound: number;
  stopAlphas: number;
}

export interface RunContext {
  readonly simStart: string;
  readonly simEnd: string;
  readonly isStart: string;
  readonly osStart: string;
  readonly universe: string;
  readonly executionPrice: ExecutionPrice;
  readonly threads: number;
  readonly feeRate: number;
  readonly reportNotional: number;
}

/** Returns, weights, turnover and drawdown are fractions, never percentage points. */
export interface SummaryMetrics {
  pnl: number;
  annualReturn: number;
  sharpe: number;
  maxDrawdown: number;
  turnover: number;
  maxWeight: number;
  avgPositionCount: number;
  minPositionCount: number;
  activeBarCount: number;
  avgMaxWeight: number;
  winRate: number;
  margin: number;
  quality: number;
  sharpe400: number;
  sharpe80: number;
  minGrossExposure: number;
  longRatio: number;
  shortRatio: number;
}

export interface YearlyMetrics {
  year: number;
  longRatio: number;
  shortRatio: number;
  pnl: number;
  annualReturn: number;
  sharpe: number;
  informationRatio: number;
  maxDrawdown: number;
  winRate: number;
  turnover: number;
  margin: number;
  fitness: number;
}

/** Aligned epoch-ms series, rebased at max(simStart, isStart); OS continues IS. */
export interface PerformanceSeries {
  timestamps: number[];
  cumulativeNet: number[];
  cumulativeGross: number[];
  drawdown: number[];
  turnover: number[];
  maxWeight: number[];
  positionCount: number[];
  osStartIndex: number;
  frozenGross?: number[];
}

export interface SignalRange { min: number; max: number }

export interface SignalPlot {
  timestamps: number[];
  symbols: string[];
  values: (number | null)[][];
  membership?: string[];
  centers: (number | null)[];
  scales: (number | null)[];
  rowStride: number;
  sampled: boolean;
  originalRows?: number;
  originalSymbols?: number;
  symbolStride?: number;
}

export interface Distribution {
  bins: number[];
  counts: number[];
  min: number;
  max: number;
  rangeLo?: number;
  rangeHi?: number;
  exampleSymbol?: (string | null)[];
  exampleTimestamp?: (number | null)[];
  exampleValue?: (number | null)[];
  edges?: number[];
  scale?: 'linear' | 'log' | 'negative-log' | 'symlog';
  linthresh?: number;
  totalCount?: number;
  linear?: Distribution;
}

export interface SignalPayload {
  plot?: SignalPlot;
  range?: SignalRange;
  distribution?: Distribution;
  logDistribution?: Distribution;
}

export interface PoolCorrelation { ew: number | null; days: number }

export interface MetaResultInfo {
  name: string;
  members: number;
  combine: string;
  fallbackBars?: number;
  firstEstimatedBar?: number;
}

export interface MissingDataDiagnostics {
  missingHeldBars: number;
  frozenGrossSum: number;
  retirements: number;
  blockedOrders: number;
  blockedTradeWeight: number;
  missingEntries: number;
  resumedHoldings: number;
  successorConversions: number;
  convertedGross: number;
  terminalFrozenGross: number;
}

export interface AlphaResult {
  id: number;
  xml?: string;
  round?: number;
  origin?: string;
  status: 'completed' | 'failed';
  error?: string;
  summary?: { is: SummaryMetrics; os: SummaryMetrics };
  yearly?: YearlyMetrics[];
  performance?: PerformanceSeries;
  signals?: SignalPayload;
  poolCorrelation?: PoolCorrelation;
  missingData?: MissingDataDiagnostics;
  meta?: MetaResultInfo[];
}

export interface SimulationResponse {
  context: RunContext;
  alphas: AlphaResult[];
  combined?: AlphaResult | null;
}

export interface RunResult extends SimulationResponse {
  sourceXml: string;
  savedSource?: string;
}

export interface EngineConfig {
  symbols: string[];
  symbolLabels?: Record<string, string>;
  simStart: string;
  simEnd: string;
  feeRate: number;
  reportNotional: number;
  barsPerYear: number;
  executionPrice: ExecutionPrice;
  executionPrices: ExecutionPrice[];
  universes: string[];
  defaultUniverse: string;
  alphasDir?: string;
  metaDir?: string;
  metaNames?: string[];
  alphaPool?: string | null;
  alphaPoolSize?: number;
  alphaPoolUniverse?: string | null;
  thinLayout?: boolean;
  T: number;
  N: number;
  columns: string[];
  loadedFields?: number;
  loadedDataBytes?: number;
}

export type SimulationOutput = 'performance' | 'signals' | 'signalSeries' | 'full';

export interface SimulationRequest {
  xml?: string;
  xmls?: string[];
  combinedOnly?: boolean;
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  threads: number;
  universe: string;
  executionPrice: ExecutionPrice;
  output: SimulationOutput;
  signalPlotSymbols?: string[];
  signalPlotMaxCells?: number;
}

export interface SearchRequest {
  xml: string;
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  threads: number;
  universe: string;
  executionPrice: ExecutionPrice;
  firstSearch: number;
  roundSize: number;
  stopRound: number;
  stopAlphas: number;
}

export type SearchEvent =
  | { type: 'search_start'; runId: string; context: RunContext }
  | { type: 'round_start'; round: number; size: number }
  | { type: 'candidate'; candidate: AlphaResult }
  | { type: 'round_end'; round: number; bestSharpe: number | null }
  | { type: 'search_complete'; stopReason: string; evaluated: number; bestXml: string }
  | { type: 'error'; error: string };

export interface SearchRound {
  round: number;
  status: 'running' | 'completed' | 'stopped';
}

export type RequestState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready' }
  | { status: 'error'; error: string };

export type SavedAlphaLevel = 'xml' | 'results' | 'positions';

export interface SavedAlphaMetadata {
  simStart: string;
  simEnd: string;
  isStart: string;
  osStart: string;
  universe: string;
  executionPrice: ExecutionPrice;
  threads: number;
}

export interface SavedAlphaRow { id: number; round: number; xml: string }

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
  context: RunContext;
  rows: AlphaResult[];
}

export interface SelectedSignalSeries {
  key: string;
  timestamps: number[];
  values: (number | null)[];
  membership: boolean[];
}

export interface PinnedSnapshot {
  id: string;
  name: string;
  color: string;
  signalSeries: { timestamps: number[]; values: (number | null)[] };
}

export type ViewMode = 'single' | 'multi' | 'detail';
