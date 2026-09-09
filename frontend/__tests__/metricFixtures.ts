import type { BacktestAlphaResult, BacktestRunResponse } from '@/types';

// Display fixtures live with the frontend and use its internal metric names.
const alpha: BacktestAlphaResult = {
  name: 'alpha_0',
  rank: 1,
  isMetrics: {
    pnl: 5,
    ann_ret: 947.5,
    sharpe: 5.06,
    turnover: 26.7,
    avgPositionCount: 3.5,
    minPositionCount: 2,
    activeBarCount: 3,
    max_weight: 60,
    margin: 0.000974,
  },
  osMetrics: {
    pnl: -2,
    ann_ret: -60.3,
    sharpe: -4.5,
    turnover: 20,
    avgPositionCount: 5.25,
    minPositionCount: 1,
    activeBarCount: 4,
    max_weight: 50,
    margin: -0.001,
  },
};

export const singleAlphaMetricFixture: BacktestRunResponse = {
  combined: null,
  alphas: [alpha],
};

export const multiAlphaMetricFixture: BacktestRunResponse = {
  combined: null,
  alphas: [
    { ...alpha, isMetrics: { ...alpha.isMetrics, sharpe: 5.9995 } },
    { ...alpha, name: 'alpha_1', rank: 2 },
  ],
  sortedBy: 'sharpe',
};
