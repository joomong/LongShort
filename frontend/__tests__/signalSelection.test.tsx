import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAlphaStore } from '@/store/alphaStore';
import { selectSignalSource } from '@/lib/signalSelection';
import SignalPanelSelector from '@/components/visualization/SignalPanelSelector';
import { runSimulate } from '@/lib/api';
import type { BacktestRunResponse, HeatmapData, StarBatchResponse } from '@/types';

vi.mock('@/lib/api', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/api')>(), runSimulate: vi.fn(),
}));
const initial = useAlphaStore.getState();
const heatmap = (value: number): HeatmapData => ({ timestamps: ['2024-01-01'], symbols: ['A'], values: [[value]] });
const result = (): BacktestRunResponse => ({
  combined: null,
  alphas: [
    { name: 'alpha_0', rank: 1, xml: '<data expr="close"/>', isMetrics: { pnl: 1, sharpe: 1 }, osMetrics: { pnl: 1, sharpe: 1 }, heatmap: heatmap(5) },
    { name: 'alpha_1', rank: 2, xml: '<data expr="-close" delay="1"/>', isMetrics: { pnl: -1, sharpe: -1 }, osMetrics: { pnl: -1, sharpe: -1 } },
  ],
  runSettings: { simStart: '2020-01-01', isStart: '2021-01-01', osStart: '2023-01-01', mode: 0, threads: 2 },
});
const response = (value: number) => ({ batches: [], yearly: [], timeseries: { timestamps: [], cumPnl: [], cumPnlGross: [], turnover: [], maxWeight: [], nonzeroCount: [], drawdown: [] }, isStartIndex: 0, osStartIndex: 0, signalPlot: heatmap(value), signalRange: { min: value, max: value }, distribution: { bins: [value], counts: [1], min: value, max: value } } satisfies StarBatchResponse);

beforeEach(() => { vi.clearAllMocks(); useAlphaStore.setState(initial, true); });

describe('selected alpha signals', () => {
  it('loads only missing selected signals using the original run dates and reuses the cache', async () => {
    useAlphaStore.getState().setResult(result());
    useAlphaStore.getState().selectSignalAlpha(1);
    vi.mocked(runSimulate).mockResolvedValue(response(-7));
    render(<SignalPanelSelector />);
    await waitFor(() => expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toEqual(heatmap(-7)));
    expect(runSimulate).toHaveBeenCalledWith('<data expr="-close" delay="1"/>', '2020-01-01', '2021-01-01', '2023-01-01', expect.any(AbortSignal), 2, 0);
    expect(selectSignalSource(useAlphaStore.getState())?.distribution).toEqual([1]);
    expect(useAlphaStore.getState().selectedSymbol).toBe('A');
    fireEvent.change(screen.getByLabelText('Signals'), { target: { value: '0' } });
    expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toEqual(heatmap(5));
    fireEvent.change(screen.getByLabelText('Signals'), { target: { value: '1' } });
    expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toEqual(heatmap(-7));
    expect(runSimulate).toHaveBeenCalledTimes(1);
  });

  it('does not attach a stale signal response to a new backtest', async () => {
    let resolve!: (value: StarBatchResponse) => void;
    vi.mocked(runSimulate).mockReturnValue(new Promise(done => { resolve = done; }));
    useAlphaStore.getState().setResult(result());
    useAlphaStore.getState().selectSignalAlpha(1);
    render(<SignalPanelSelector />);
    await waitFor(() => expect(runSimulate).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(runSimulate).mock.calls[0][4];
    act(() => useAlphaStore.getState().setResult(result()));
    await act(async () => { resolve(response(-999)); });
    expect(signal?.aborted).toBe(true);
    expect(useAlphaStore.getState().result?.alphas[1].heatmap).toBeUndefined();
    expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toEqual(heatmap(5));
  });

  it('selects combined and individual payloads without borrowing another alpha signal', () => {
    const data = result();
    data.combined = { ...data.alphas[0], name: 'combined', heatmap: heatmap(2) };
    useAlphaStore.getState().setResult(data);
    expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toEqual(heatmap(2));
    useAlphaStore.getState().selectSignalAlpha(1);
    expect(selectSignalSource(useAlphaStore.getState())?.heatmap).toBeUndefined();
  });

  it('bounds cached alpha signals without removing their computed metrics', () => {
    const data = result();
    data.alphas = Array.from({ length: 7 }, (_, index) => ({ ...data.alphas[1], name: `alpha_${index}`, rank: index + 1 }));
    useAlphaStore.getState().setResult(data);
    const revision = useAlphaStore.getState().resultRevision;
    for (let index = 0; index < 5; index++) useAlphaStore.getState().cacheAlphaSignals(index, { heatmap: heatmap(index) }, revision);
    useAlphaStore.getState().selectSignalAlpha(0); // Keep the recently viewed payload.
    useAlphaStore.getState().cacheAlphaSignals(5, { heatmap: heatmap(5) }, revision);
    const alphas = useAlphaStore.getState().result!.alphas;
    expect(alphas.filter(alpha => alpha.heatmap)).toHaveLength(5);
    expect(alphas[0].heatmap).toEqual(heatmap(0));
    expect(alphas[1].heatmap).toBeUndefined();
    expect(alphas[1].isMetrics).toEqual(data.alphas[1].isMetrics);
    expect(alphas[5].heatmap).toEqual(heatmap(5));
  });

});
