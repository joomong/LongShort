import { describe, it, expect } from 'vitest';

import { adaptSimulateResponse } from '@/lib/api';
import type { StarBatchResponse } from '@/types';

function makeBaseTimeseries() {
  return {
    timestamps: ['2024-01-01', '2024-01-02', '2024-01-03', '2024-01-04'],
    cumPnl: [0.0, 0.01, 0.015, 0.03],
    cumPnlGross: [0.0, 0.011, 0.016, 0.031],
    turnover: [0.1, 0.2, 0.1, 0.3],
    maxWeight: [20, 30, 25, 40],
    nonzeroCount: [1, 2, 2, 3],
    drawdown: [0.0, -0.2, -0.1, -0.3],
  };
}

function makeSummary() {
  return {
    return: 12,
    sharpe: 1.5,
    mdd: -3,
    turnover: 20,
    maxWeight: 40,
    minBet: 3.5,
    pnl: 5,
    winRate: 50,
    margin: 0.01,
    longRatio: 0.6,
    shortRatio: 0.4,
    nonzeroPosition: 2,
    avgMaxBet: 20,
    minBetMin: 1,
    quality: 0.9,
    sharpe400: 1.4,
    sharpe80: 1.1,
    minsoc: 80,
  };
}

describe('adaptSimulateResponse', () => {
  it('builds per-alpha detail payloads without Combined row', () => {
    const ts0 = makeBaseTimeseries();
    const ts1 = {
      ...makeBaseTimeseries(),
      cumPnl: [0.0, 0.005, 0.02, 0.025],
    };
    const s0 = makeSummary();
    const s1 = { ...makeSummary(), sharpe: 1.8 };

    const resp: StarBatchResponse = {
      batches: [
        {
          index: 3,
          xml: '<data expr="close" delay="1"/>',
          summary: { is: s0, os: s0 },
          yearly: [{ year: 2024, longSM: 0.6, shortSM: 0.4, pnl: 1.2, annRet: 12.3, sharpe: 1.5, maxDD: -3.0, dailyTvr: 0.2, margin: 0.01 }],
          timeseries: ts0,
          isStartIndex: 0,
          osStartIndex: 2,
        },
        {
          index: 7,
          xml: '<data expr="open" delay="1"/>',
          summary: { is: s1, os: s1 },
          yearly: [{ year: 2024, longSM: 0.55, shortSM: 0.45, pnl: 1.4, annRet: 14.1, sharpe: 1.8, maxDD: -2.0, dailyTvr: 0.3, margin: 0.02 }],
          timeseries: ts1,
          isStartIndex: 0,
          osStartIndex: 3,
        },
      ],
      yearly: [],
      timeseries: makeBaseTimeseries(),
      isStartIndex: 0,
      osStartIndex: 2,
    };

    const adapted = adaptSimulateResponse(resp);

    // No Combined row — just 2 alphas directly
    expect(adapted.alphas).toHaveLength(2);
    expect(adapted.alphas[0].name).toBe('alpha_3');
    expect(adapted.alphas[0].rank).toBe(1);
    expect(adapted.alphas[0].xml).toBe('<data expr="close" delay="1"/>');
    expect(adapted.alphas[0].isEquitySeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.01,
    });
    expect(adapted.alphas[0].isMaxWeightSeries?.['2024-01-02']).toBeCloseTo(30, 9);
    expect(adapted.alphas[0].osEquitySeries).toEqual({
      '2024-01-03': 0.015,
      '2024-01-04': 0.03,
    });
    expect(adapted.alphas[0].yearly?.[0].year).toBe(2024);

    expect(adapted.alphas[1].name).toBe('alpha_7');
    expect(adapted.alphas[1].rank).toBe(2);
    expect(adapted.alphas[1].xml).toBe('<data expr="open" delay="1"/>');
    expect(adapted.alphas[1].osStartDate).toBe('2024-01-04');
    expect(adapted.alphas[1].isRetSeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.005,
      '2024-01-03': 0.015,
    });
    expect(adapted.alphas[1].osRetSeries?.['2024-01-04']).toBeCloseTo(0.005, 9);

    // Combined uses best (first batch)
    expect(adapted.combined).not.toBeNull();
    expect(adapted.combined!.isRetSeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.01,
    });
    expect(adapted.combined!.osRetSeries?.['2024-01-03']).toBeCloseTo(0.005, 9);
    expect(adapted.combined!.osRetSeries?.['2024-01-04']).toBeCloseTo(0.015, 9);
  });

  it('mixes per-batch and top-level fallback detail fields correctly', () => {
    const s = makeSummary();
    const topLevelTs = makeBaseTimeseries();
    const customTs = {
      ...makeBaseTimeseries(),
      cumPnl: [0.0, 0.02, 0.025, 0.04],
    };
    const resp: StarBatchResponse = {
      batches: [
        { index: 0, summary: { is: s, os: s } }, // top-level fallback
        { index: 1, summary: { is: s, os: s }, timeseries: customTs, isStartIndex: 1, osStartIndex: 3 },
      ],
      yearly: [{ year: 2024, longSM: 0.6, shortSM: 0.4, pnl: 1.0, annRet: 10.0, sharpe: 1.0, maxDD: -2.0, dailyTvr: 0.2, margin: 0.01 }],
      timeseries: topLevelTs,
      isStartIndex: 0,
      osStartIndex: 2,
    };

    const adapted = adaptSimulateResponse(resp);

    expect(adapted.alphas).toHaveLength(2);
    expect(adapted.alphas[0].rank).toBe(1);
    expect(adapted.alphas[0].isRetSeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.01,
    });
    expect(adapted.alphas[1].isRetSeries?.['2024-01-02']).toBeCloseTo(0.02, 9);
    expect(adapted.alphas[1].osRetSeries).toEqual({
      '2024-01-04': 0.015,
    });
    expect(adapted.alphas[1].isRetSeries?.['2024-01-03']).toBeCloseTo(0.005, 9);
  });

  it('returns empty output for empty batches', () => {
    const adapted = adaptSimulateResponse({
      batches: [],
      yearly: [],
      timeseries: makeBaseTimeseries(),
      isStartIndex: 0,
      osStartIndex: 2,
    });

    expect(adapted.combined).toBeNull();
    expect(adapted.alphas).toEqual([]);
  });

  it('uses resp.combined when present (actual combined from C++ engine)', () => {
    const s = makeSummary();
    const combinedSummary = { ...makeSummary(), sharpe: 2.5, return: 25 };
    const combinedTs = {
      ...makeBaseTimeseries(),
      cumPnl: [0.0, 0.03, 0.05, 0.08],
    };

    const resp: StarBatchResponse = {
      batches: [
        {
          index: 0,
          xml: '<data expr="close" delay="1"/>',
          summary: { is: s, os: s },
          timeseries: makeBaseTimeseries(),
          isStartIndex: 0,
          osStartIndex: 2,
        },
        {
          index: 1,
          xml: '<data expr="close" delay="2"/>',
          summary: { is: s, os: s },
          timeseries: makeBaseTimeseries(),
          isStartIndex: 0,
          osStartIndex: 2,
        },
      ],
      yearly: [],
      timeseries: makeBaseTimeseries(),
      isStartIndex: 0,
      osStartIndex: 2,
      combined: {
        index: -1,
        summary: { is: combinedSummary, os: combinedSummary },
        timeseries: combinedTs,
        isStartIndex: 0,
        osStartIndex: 2,
      },
    };

    const adapted = adaptSimulateResponse(resp);

    // Combined should use the actual combined data, not best batch
    expect(adapted.combined).not.toBeNull();
    expect(adapted.combined!.isMetrics.sharpe).toBe(2.5);
    expect(adapted.combined!.isMetrics.ann_ret).toBeCloseTo(25, 5);
    expect(adapted.combined!.isRetSeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.03,
    });
    expect(adapted.combined!.osRetSeries?.['2024-01-03']).toBeCloseTo(0.02, 9);
    expect(adapted.combined!.osRetSeries?.['2024-01-04']).toBeCloseTo(0.03, 9);
  });

  it('falls back to best batch when resp.combined is absent', () => {
    const s = makeSummary();
    const resp: StarBatchResponse = {
      batches: [
        {
          index: 0,
          summary: { is: s, os: s },
          timeseries: makeBaseTimeseries(),
          isStartIndex: 0,
          osStartIndex: 2,
        },
      ],
      yearly: [],
      timeseries: makeBaseTimeseries(),
      isStartIndex: 0,
      osStartIndex: 2,
    };

    const adapted = adaptSimulateResponse(resp);

    // Should fall back to batches[0]
    expect(adapted.combined).not.toBeNull();
    expect(adapted.combined!.isMetrics.sharpe).toBe(s.sharpe);
    expect(adapted.combined!.isRetSeries).toEqual({
      '2024-01-01': 0,
      '2024-01-02': 0.01,
    });
  });

  it('passes through signal fields for single alpha', () => {
    const s = makeSummary();
    const resp: StarBatchResponse = {
      batches: [
        {
          index: 0,
          xml: '<data expr="close"/>',
          summary: { is: s, os: s },
          timeseries: makeBaseTimeseries(),
          isStartIndex: 0,
          osStartIndex: 2,
        },
      ],
      yearly: [],
      timeseries: makeBaseTimeseries(),
      isStartIndex: 0,
      osStartIndex: 2,
      signalPlot: { timestamps: ['2024-01-01'], symbols: ['BTC'], values: [[0.5]] },
      signalRange: { min: -1, max: 1 },
      distribution: { bins: [0.1], counts: [10], min: 0, max: 1 },
    };

    const adapted = adaptSimulateResponse(resp);

    expect(adapted.heatmap).toEqual({ timestamps: ['2024-01-01'], symbols: ['BTC'], values: [[0.5]] });
    expect(adapted.signalRange).toEqual({ min: -1, max: 1 });
    expect(adapted.distribution).toEqual([10]);
  });
});
