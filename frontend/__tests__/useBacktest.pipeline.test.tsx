import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';

import { useAlphaStore } from '@/store/alphaStore';
import { useBacktest } from '@/hooks/useBacktest';

vi.mock('@/lib/api', () => ({
  runSimulate: vi.fn(),
  runGAStream: vi.fn(),
  adaptSingleResponse: vi.fn(),
  adaptBatchResponse: vi.fn(),
  adaptGACompleteToResponse: vi.fn(),
  getConfig: vi.fn(),
  saveAlpha: vi.fn(),
  getAlphaList: vi.fn(),
  getAlpha: vi.fn(),
  deleteAlpha: vi.fn(),
}));

import { runSimulate, adaptSingleResponse, adaptBatchResponse } from '@/lib/api';
import type { StarSimulateResponse } from '@/types';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  vi.clearAllMocks();
  useAlphaStore.setState(initialState, true);
});

function renderHookHarness(): ReturnType<typeof useBacktest> {
  let hookApi: ReturnType<typeof useBacktest> | null = null;

  function Harness() {
    hookApi = useBacktest();
    return null;
  }

  render(<Harness />);

  if (!hookApi) {
    throw new Error('useBacktest hook did not initialize');
  }
  return hookApi as ReturnType<typeof useBacktest>;
}

describe('useBacktest pipeline behavior', () => {
  it('run() calls runSimulate and adapts single response', async () => {
    const starResponse = {
      summary: { is: { return: 1.0, sharpe: 0.5, mdd: -10, turnover: 20, maxWeight: 0.08, minBet: 3, pnl: 100, winRate: 0.55, margin: 0.02, longRatio: 0.5, shortRatio: 0.5, nonzeroPosition: 15 },
                 os: { return: 0.8, sharpe: 0.4, mdd: -12, turnover: 18, maxWeight: 0.09, minBet: 2, pnl: 80, winRate: 0.52, margin: 0.01, longRatio: 0.5, shortRatio: 0.5, nonzeroPosition: 14 } },
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 100,
      signalPlot: { timestamps: ['2024-01-01'], symbols: ['BTCUSDT'], values: [[0.5]] },
      signalRange: { min: -1, max: 1 },
    };

    const adapted = {
      combined: { isMetrics: { pnl: 100, sharpe: 0.5 }, osMetrics: { pnl: 80, sharpe: 0.4 } },
      alphas: [{ name: 'alpha_0', rank: 1, isMetrics: { pnl: 100, sharpe: 0.5 }, osMetrics: { pnl: 80, sharpe: 0.4 } }],
      heatmap: { timestamps: ['2024-01-01'], symbols: ['BTCUSDT'], values: [[0.5]] },
      signalRange: { min: -1, max: 1 },
      distribution: null,
    };

    vi.mocked(runSimulate).mockResolvedValue(starResponse as unknown as StarSimulateResponse);
    vi.mocked(adaptSingleResponse).mockReturnValue(adapted as never);

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    expect(runSimulate).toHaveBeenCalledTimes(1);
    expect(adaptSingleResponse).toHaveBeenCalledWith(starResponse);

    const state = useAlphaStore.getState();
    expect(state.isRunning).toBe(false);
    expect(state.backtestError).toBeNull();
    expect(state.result).toEqual(adapted);
    expect(state.selectedSymbol).toBe('BTCUSDT');
    expect(state.selectedTimestamp).toBe('2024-01-01');
  });

  it('run() adapts batch response when batches present', async () => {
    const batchResponse = {
      batches: [{ index: 0, summary: { is: {}, os: {} } }],
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 100,
    };

    const adapted = {
      combined: null,
      alphas: [{ name: 'alpha_0', rank: 1, isMetrics: {}, osMetrics: {} }],
      heatmap: null,
      signalRange: null,
      distribution: null,
    };

    vi.mocked(runSimulate).mockResolvedValue(batchResponse as never);
    vi.mocked(adaptBatchResponse).mockReturnValue(adapted as never);

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    expect(adaptBatchResponse).toHaveBeenCalledWith(batchResponse);
    expect(useAlphaStore.getState().result).toEqual(adapted);
  });

  it('handles thrown transport errors from runSimulate', async () => {
    vi.mocked(runSimulate).mockRejectedValue(new Error('network down'));

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    const state = useAlphaStore.getState();
    expect(state.backtestError).toContain('network down');
    expect(state.isRunning).toBe(false);
  });
});
