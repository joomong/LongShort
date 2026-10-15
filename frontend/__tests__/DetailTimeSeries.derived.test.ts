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
      '2024-01-02': 50,
      '2024-01-03': 20,
      '2024-01-04': 10,
    };

    const out = buildPnlBundleFromRet(ret, turnover, 100000, 0.001);

    expect(out.pnlData).toHaveLength(3);
    expect(out.pnlData[0].date).toBe('2024-01-02');
    expect(out.pnlData[0].net).toBeCloseTo(1000, 9);
    expect(out.pnlData[0].gross).toBeCloseTo(1050, 9);
    // Drawdown equity follows gross: 0.0105 - 0.0198 + 0.0301.
    expect(out.grossEquityData[2].value).toBeCloseTo(102080, 9);
  });

  it('prefers the engine gross return series over fee reconstruction', () => {
    const ret = { '2024-01-02': 0.01, '2024-01-03': -0.02 };
    const turnover = { '2024-01-02': 50, '2024-01-03': 20 };
    const gross = { '2024-01-02': 0.012, '2024-01-03': -0.015 };
    const out = buildPnlBundleFromRet(ret, turnover, 100000, 0.001, gross);
    expect(out.pnlData[1].net).toBeCloseTo(-1000, 9);
    expect(out.pnlData[1].gross).toBeCloseTo(-300, 9);
    expect(out.grossEquityData.map(p => p.value)).toEqual([101200, 99700]);
    const mdd = buildMddDataFromEquity(out.grossEquityData, 100000);
    expect(mdd[1].value).toBeCloseTo(-1.5, 9);
  });

  it('marks gross series unknown when turnover is missing on any bar', () => {
    const ret = {
      '2024-01-05': 0.005,
      '2024-01-06': 0.0,
      '2024-01-07': -0.01,
    };
    const turnover = {
      '2024-01-05': 15,
      '2024-01-06': null,
      '2024-01-07': 5,
    };

    const out = buildPnlBundleFromRet(ret, turnover, 100000, 0.001);

    expect(Number.isFinite(out.pnlData[0].gross)).toBe(true);
    expect(Number.isFinite(out.pnlData[1].gross)).toBe(false);
    expect(Number.isFinite(out.pnlData[2].gross)).toBe(false);
  });

  it('builds additive drawdown against the initial book', () => {
    const mdd = buildMddDataFromEquity([
      { date: '2024-01-02', value: 101000 },
      { date: '2024-01-03', value: 98980 },
      { date: '2024-01-04', value: 101949.4 },
    ], 100000);

    expect(mdd[0].value).toBeCloseTo(0.0, 9);
    expect(mdd[1].value).toBeCloseTo(-2.02, 9);
    expect(mdd[2].value).toBeCloseTo(0.0, 9);
  });

  it('includes a loss on the first bar in additive drawdown', () => {
    const rows = buildMddDataFromEquity([
      { date: '2024-01-02', value: 90000 },
      { date: '2024-01-03', value: 95000 },
      { date: '2024-01-04', value: 85000 },
    ], 100000);
    expect(rows.map(row => row.value)).toEqual([-10, -5, -15]);
  });

  it('uses the fixed book after gains and below zero equity', () => {
    const rows = buildMddDataFromEquity([
      { date: '2024-01-02', value: 200000 },
      { date: '2024-01-03', value: 150000 },
      { date: '2024-01-04', value: -10000 },
    ], 100000);
    expect(rows.map(row => row.value)).toEqual([0, -50, -210]);
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
