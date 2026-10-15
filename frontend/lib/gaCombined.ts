import type { BacktestAlphaResult } from '@/types';

/** Accumulate in candidate order, independent of completion order or table sorting. */
export function gaCombinedGroup(alphas: BacktestAlphaResult[], tab: 'stats' | number) {
  const eligible = alphas.filter(alpha => !alpha.error && alpha.xml && (tab === 'stats' || alpha.round === tab))
    .sort((a, b) => (a.candidateId ?? 0) - (b.candidateId ?? 0));
  return eligible.length < 2 ? { key: '', xmls: [] as string[] } : {
    key: eligible.map(alpha => alpha.candidateId).join(','),
    xmls: eligible.map(alpha => alpha.xml!),
  };
}

export function pendingGACombined(): BacktestAlphaResult {
  const metrics = { pnl: NaN, sharpe: NaN, mdd: NaN, turnover: NaN, avgPositionCount: NaN, max_weight: NaN };
  return { name: 'combined', rank: 0, isMetrics: metrics, osMetrics: metrics, detailLoaded: false };
}
