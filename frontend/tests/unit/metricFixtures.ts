import type { AlphaResult, EngineConfig, RunContext, RunResult, SummaryMetrics } from '@/types';

export const contextFixture: RunContext = {
  simStart: '2020-01-01',
  simEnd: '2025-12-31',
  isStart: '2021-01-01',
  osStart: '2024-01-01',
  universe: 'TOP500',
  executionPrice: 'vwap',
  threads: 4,
  feeRate: 0.00015,
  reportNotional: 20_000_000,
};
export const engineConfigFixture: EngineConfig = {
  symbols: ['A', 'B'],
  simStart: '2020-01-01',
  simEnd: '2025-12-31',
  feeRate: 0.00015,
  barsPerYear: 252,
  executionPrice: 'close',
  executionPrices: ['close', 'ohlc4', 'vwap'],
  universes: ['TOP3000', 'TOP500'],
  defaultUniverse: 'TOP3000',
  reportNotional: 20_000_000,
  T: 100,
  N: 2,
  columns: ['close', 'volume'],
  alphasDir: '/data/alphas',
};
export const summaryFixture: SummaryMetrics = {
  pnl: 0.05,
  annualReturn: 0.9475,
  sharpe: 5.06,
  maxDrawdown: -0.03,
  turnover: 0.267,
  maxWeight: 0.6,
  avgPositionCount: 3.5,
  minPositionCount: 2,
  activeBarCount: 3,
  avgMaxWeight: 0.02875,
  winRate: 0.6,
  margin: 0.000974,
  quality: 0.9,
  sharpe400: 1.234,
  sharpe80: -0.236,
  minGrossExposure: 1,
  longRatio: 0.5,
  shortRatio: 0.5,
};
export function candidateFixture(id: number, round = 1, error?: string): AlphaResult {
  return {
    id,
    round,
    xml: `<data expr="close" delay="1"/><tsmean days="${id + 1}"/>`,
    status: error ? 'failed' : 'completed',
    ...(error
      ? { error }
      : {
          summary: {
            is: { ...summaryFixture },
            os: {
              ...summaryFixture,
              pnl: -0.02,
              annualReturn: -0.603,
              sharpe: -4.5,
              avgPositionCount: 5.25,
              minPositionCount: 1,
            },
          },
        }),
  };
}
export const singleAlphaMetricFixture: RunResult = {
  context: contextFixture,
  sourceXml: '<data expr="close"/>',
  alphas: [candidateFixture(0)],
  combined: null,
};
export const multiAlphaMetricFixture: RunResult = {
  context: contextFixture,
  sourceXml: '<data expr="close"/><tsmean days="1|2"/>',
  alphas: [
    {
      ...candidateFixture(0),
      summary: { is: { ...summaryFixture, sharpe: 5.9995 }, os: candidateFixture(0).summary!.os },
    },
    candidateFixture(1),
  ],
  combined: null,
};
