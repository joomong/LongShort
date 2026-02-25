import { describe, it, expect } from 'vitest';

import {
  buildMddDataFromEquity,
  buildPnlBundleFromRet,
  seriesToChartData,
} from '@/components/visualization/DetailTimeSeries';

describe('DetailTimeSeries derived data', () => {
  it('builds net/gross pnl series from return + turnover inputs', () => {
    const ret = {
      '2024-01-02': 0.01,
      '2024-01-03': -0.02,
      '2024-01-04': 0.03,
    };
    const turnover = {
      '2024-01-02': 0.5,
      '2024-01-03': 0.2,
      '2024-01-04': 0.1,
    };

    const out = buildPnlBundleFromRet(ret, turnover, 100000, 0.001);

    expect(out.pnlData).toHaveLength(3);
    expect(out.pnlData[0].date).toBe('2024-01-02');
    expect(out.pnlData[0].net).toBeCloseTo(1000, 9);
    expect(out.pnlData[0].gross).toBeCloseTo(1050, 9);
    expect(out.netEquityData[2].value).toBeCloseTo(101949.4, 9);
  });

  it('marks gross series unknown when turnover is missing on any bar', () => {
    const ret = {
      '2024-01-05': 0.005,
      '2024-01-06': 0.0,
      '2024-01-07': -0.01,
    };
    const turnover = {
      '2024-01-05': 0.15,
      '2024-01-06': null,
      '2024-01-07': 0.05,
    };

    const out = buildPnlBundleFromRet(ret, turnover, 100000, 0.001);

    expect(Number.isFinite(out.pnlData[0].gross)).toBe(true);
    expect(Number.isFinite(out.pnlData[1].gross)).toBe(false);
    expect(Number.isFinite(out.pnlData[2].gross)).toBe(false);
  });

  it('builds running MDD percent from equity series', () => {
    const mdd = buildMddDataFromEquity([
      { date: '2024-01-02', value: 101000 },
      { date: '2024-01-03', value: 98980 },
      { date: '2024-01-04', value: 101949.4 },
    ]);

    expect(mdd[0].value).toBeCloseTo(0.0, 9);
    expect(mdd[1].value).toBeCloseTo(-2.0, 9);
    expect(mdd[2].value).toBeCloseTo(0.0, 9);
  });

  it('converts sparse nullable series to sorted finite chart rows', () => {
    const rows = seriesToChartData({
      '2024-01-03': 0.2,
      '2024-01-02': null,
      '2024-01-04': 0.4,
    });

    expect(rows).toEqual([
      { date: '2024-01-03', value: 0.2 },
      { date: '2024-01-04', value: 0.4 },
    ]);
  });
});
