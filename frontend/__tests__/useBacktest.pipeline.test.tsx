import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';

import { useAlphaStore } from '@/store/alphaStore';
import { useBacktest } from '@/hooks/useBacktest';

vi.mock('@/lib/api', () => ({
  runSimulate: vi.fn(),
  runGAStream: vi.fn(),
  adaptSimulateResponse: vi.fn(),
  adaptGACompleteToResponse: vi.fn(),
  getConfig: vi.fn(),
  saveAlpha: vi.fn(),
  getAlphaList: vi.fn(),
  getAlpha: vi.fn(),
  deleteAlpha: vi.fn(),
}));

import { runSimulate, runGAStream, adaptSimulateResponse } from '@/lib/api';
import type { StarGAEvent } from '@/types';

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useBacktest pipeline behavior', () => {
  it('run() calls runSimulate and adapts response via adaptSimulateResponse', async () => {
    useAlphaStore.setState({ xmlCode: '<data expr="foo"/>' });

    const starResponse = {
      batches: [{
        index: 0,
        xml: '<data expr="foo"/>',
        summary: { is: { return: 1.0, sharpe: 0.5, mdd: -10, turnover: 20, maxWeight: 0.08, minBet: 3, pnl: 100, winRate: 0.55, margin: 0.02, longRatio: 0.5, shortRatio: 0.5, nonzeroPosition: 15 },
                   os: { return: 0.8, sharpe: 0.4, mdd: -12, turnover: 18, maxWeight: 0.09, minBet: 2, pnl: 80, winRate: 0.52, margin: 0.01, longRatio: 0.5, shortRatio: 0.5, nonzeroPosition: 14 } },
      }],
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 100,
      signalPlot: { timestamps: ['2024-01-01'], symbols: ['BTCUSDT'], values: [[0.5]] },
      signalRange: { min: -1, max: 1 },
    };

    const adapted = {
      combined: { isMetrics: { pnl: 100, sharpe: 0.5 }, osMetrics: { pnl: 80, sharpe: 0.4 } },
      alphas: [{ name: 'alpha_0', rank: 1, isMetrics: { pnl: 100, sharpe: 0.5 }, osMetrics: { pnl: 80, sharpe: 0.4 }, xml: '<data expr="foo"/>' }],
      heatmap: { timestamps: ['2024-01-01'], symbols: ['BTCUSDT'], values: [[0.5]] },
      signalRange: { min: -1, max: 1 },
      distribution: null,
    };

    vi.mocked(runSimulate).mockResolvedValue(starResponse as never);
    vi.mocked(adaptSimulateResponse).mockReturnValue(adapted as never);

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    expect(runSimulate).toHaveBeenCalledTimes(1);
    const singleRunArgs = vi.mocked(runSimulate).mock.calls[0];
    expect(singleRunArgs?.[0]).toBe('<data expr="foo"/>');
    expect(singleRunArgs?.[4]).toBeInstanceOf(AbortSignal);
    expect(singleRunArgs?.[5]).toBe(useAlphaStore.getState().settings.nJobs);
    expect(singleRunArgs?.[6]).toBe(1);
    expect(adaptSimulateResponse).toHaveBeenCalledWith(starResponse);

    const state = useAlphaStore.getState();
    expect(state.isRunning).toBe(false);
    expect(state.backtestError).toBeNull();
    expect(state.result).toEqual(adapted);
    expect(state.result?.alphas?.[0]?.xml).toBe('<data expr="foo"/>');
    expect(state.selectedSymbol).toBe('BTCUSDT');
    expect(state.selectedTimestamp).toBe('2024-01-01');
  });

  it('run() adapts multi-batch response via adaptSimulateResponse', async () => {
    const batchResponse = {
      batches: [
        { index: 0, summary: { is: {}, os: {} }, xml: '<best/>' },
        { index: 1, summary: { is: {}, os: {} }, xml: '<alpha-1/>' },
      ],
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 100,
    };

    const adapted = {
      combined: null,
      alphas: [
        { name: 'alpha_0', rank: 1, xml: '<best/>', isMetrics: {}, osMetrics: {} },
        { name: 'alpha_1', rank: 2, xml: '<alpha-1/>', isMetrics: {}, osMetrics: {} },
      ],
      heatmap: null,
      signalRange: null,
      distribution: null,
    };

    vi.mocked(runSimulate).mockResolvedValue(batchResponse as never);
    vi.mocked(adaptSimulateResponse).mockReturnValue(adapted as never);

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    const batchRunArgs = vi.mocked(runSimulate).mock.calls[0];
    expect(batchRunArgs?.[4]).toBeInstanceOf(AbortSignal);
    expect(adaptSimulateResponse).toHaveBeenCalledWith(batchResponse);
    expect(useAlphaStore.getState().result).toEqual(adapted);
    expect(useAlphaStore.getState().result?.alphas?.[1]?.xml).toBe('<alpha-1/>');
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

  it('ignores stale completion from an earlier run when a newer run has started', async () => {
    const firstResp = {
      batches: [{ index: 0, summary: { is: {}, os: {} } }],
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 1,
    };
    const secondResp = {
      batches: [{ index: 0, summary: { is: {}, os: {} } }],
      yearly: [],
      timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] },
      isStartIndex: 0,
      osStartIndex: 1,
    };

    const firstAdapted = {
      combined: null,
      alphas: [{ name: 'alpha_old', rank: 1, isMetrics: {}, osMetrics: {} }],
      heatmap: null,
      signalRange: null,
      distribution: null,
    };
    const secondAdapted = {
      combined: null,
      alphas: [{ name: 'alpha_new', rank: 1, isMetrics: {}, osMetrics: {} }],
      heatmap: null,
      signalRange: null,
      distribution: null,
    };

    const first = deferred<typeof firstResp>();
    const second = deferred<typeof secondResp>();

    vi.mocked(runSimulate)
      .mockImplementationOnce(() => first.promise as never)
      .mockImplementationOnce(() => second.promise as never);
    vi.mocked(adaptSimulateResponse).mockImplementation((resp: unknown) => {
      return (resp === firstResp ? firstAdapted : secondAdapted) as never;
    });

    const hook = renderHookHarness();

    const p1 = hook.run();
    const p2 = hook.run();

    second.resolve(secondResp);
    await act(async () => {
      await p2;
    });

    first.resolve(firstResp);
    await act(async () => {
      await p1;
    });

    const state = useAlphaStore.getState();
    expect(state.result?.alphas?.[0]?.name).toBe('alpha_new');
  });

  it('forwards D0 and the selected workers and remembers them with the result', async () => {
    useAlphaStore.getState().setSettings({ barInterval: 'D0', nJobs: 3 });
    vi.mocked(runSimulate).mockResolvedValue({ batches: [], yearly: [], timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] }, isStartIndex: 0, osStartIndex: 0 });
    vi.mocked(adaptSimulateResponse).mockReturnValue({ combined: null, alphas: [] });
    const hook = renderHookHarness();
    await act(async () => { await hook.run(); });
    expect(vi.mocked(runSimulate).mock.calls[0][5]).toBe(3);
    expect(vi.mocked(runSimulate).mock.calls[0][6]).toBe(0);
    expect(useAlphaStore.getState().result?.runSettings).toMatchObject({ mode: 0, threads: 3 });
  });

  it('forwards the selected universe and remembers it with the result', async () => {
    useAlphaStore.getState().setSettings({ universe: 'TOP1000' });
    vi.mocked(runSimulate).mockResolvedValue({ batches: [], yearly: [], timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] }, isStartIndex: 0, osStartIndex: 0 });
    vi.mocked(adaptSimulateResponse).mockReturnValue({ combined: null, alphas: [] });
    const hook = renderHookHarness();
    await act(async () => { await hook.run(); });
    expect(vi.mocked(runSimulate).mock.calls[0][7]).toEqual({ universe: 'TOP1000' });
    expect(useAlphaStore.getState().result?.runSettings).toMatchObject({ universe: 'TOP1000' });
  });

  it('Stop aborts GA transport and ignores late completion and error events', async () => {
    let observer!: (event: StarGAEvent) => void;
    const completion = deferred<void>();
    vi.mocked(runGAStream).mockImplementation(async (...args) => {
      observer = args[7];
      await completion.promise;
    });
    const originalXml = '<data expr="close" delay="2"/>';
    useAlphaStore.getState().setXmlCode(originalXml);
    useAlphaStore.getState().setSettings({ barInterval: 'D0', nJobs: 3 });
    const hook = renderHookHarness();
    let running!: Promise<void>;
    act(() => { running = hook.runGA(); });
    const abort = vi.mocked(runGAStream).mock.calls[0][8];
    expect(vi.mocked(runGAStream).mock.calls[0][9]).toBe(0);
    expect(vi.mocked(runGAStream).mock.calls[0][6]).toBe(3);
    act(() => { hook.stop(); });
    expect(abort?.aborted).toBe(true);
    await act(async () => {
      observer({ type: 'complete', bestXml: '<late/>', batches: [], yearly: [], timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] }, isStartIndex: 0, osStartIndex: 0 });
      observer({ type: 'error', error: 'late error' });
      completion.resolve();
      await running;
    });
    expect(useAlphaStore.getState().result).toBeNull();
    expect(useAlphaStore.getState().xmlCode).toBe(originalXml);
    expect(useAlphaStore.getState().gaProgress).toBeNull();
    expect(useAlphaStore.getState().backtestError).toBeNull();
  });

});
