import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRunStore } from '@/runStore';
import { useResultData } from '@/useResultData';
import { getConfig, getSavedRow, requestSimulation } from '@/api';
import type { AlphaResult, SimulationResponse } from '@/types';
import { candidateFixture, contextFixture, engineConfigFixture } from './metricFixtures';
import { singleResponseFixture, multiResponseFixture } from './pipelineFixtures';
vi.mock('@/api', () => ({
  getConfig: vi.fn(),
  getSavedRow: vi.fn(),
  getSavedBatch: vi.fn(),
  requestSimulation: vi.fn(),
}));
const initial = useRunStore.getState();
function result(alpha: AlphaResult = candidateFixture(0), context = contextFixture) {
  return { context, sourceXml: alpha.xml!, alphas: [alpha], combined: null };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  vi.clearAllMocks();
  useRunStore.setState(initial, true);
  useRunStore.getState().setConfig(engineConfigFixture);
});
describe('active result data controller', () => {
  it('keeps pending performance requests alive when only signals change', async () => {
    const pending = deferred<SimulationResponse>();
    vi.mocked(requestSimulation).mockReturnValue(pending.promise);
    useRunStore.setState({ result: result(), viewMode: 'detail', selectedAlphaIndex: 0 });
    renderHook(() => useResultData());
    await waitFor(() => expect(requestSimulation).toHaveBeenCalledOnce());
    const signal = vi.mocked(requestSimulation).mock.calls[0][1];
    const signals = singleResponseFixture.alphas[0].signals!;
    act(() => useRunStore.getState().cacheSignals(0, signals, useRunStore.getState().resultRevision));
    expect(signal?.aborted).toBe(false);
    expect(requestSimulation).toHaveBeenCalledOnce();
    await act(async () => pending.resolve(singleResponseFixture));
    await waitFor(() => expect(useRunStore.getState().result?.alphas[0].performance).toBeDefined());
    expect(useRunStore.getState().result?.alphas[0].signals).toBe(signals);
  });
  it('keeps pending signal requests alive when only performance changes', async () => {
    const pending = deferred<SimulationResponse>();
    vi.mocked(requestSimulation).mockReturnValue(pending.promise);
    useRunStore.setState({ result: result(), viewMode: 'single', selectedSignalAlphaIndex: 0 });
    renderHook(() => useResultData());
    await waitFor(() => expect(requestSimulation).toHaveBeenCalledOnce());
    const signal = vi.mocked(requestSimulation).mock.calls[0][1];
    act(() => useRunStore.getState().cachePerformance(
      0, singleResponseFixture.alphas[0], useRunStore.getState().resultRevision,
    ));
    expect(signal?.aborted).toBe(false);
    expect(requestSimulation).toHaveBeenCalledOnce();
    await act(async () => pending.resolve(singleResponseFixture));
    await waitFor(() => expect(useRunStore.getState().result?.alphas[0].signals?.plot).toBeDefined());
    expect(useRunStore.getState().result?.alphas[0].performance).toBeDefined();
    expect(vi.mocked(requestSimulation).mock.calls.filter(([request]) => request.output === 'signals')).toHaveLength(1);
  });
  it('loads only active performance using immutable execution settings', async () => {
    useRunStore.setState({
      result: result(),
      viewMode: 'detail',
      selectedAlphaIndex: 0,
      selectedSignalAlphaIndex: 0,
    });
    vi.mocked(requestSimulation).mockResolvedValue(singleResponseFixture);
    renderHook(() => useResultData());
    await waitFor(() => expect(useRunStore.getState().result?.alphas[0].performance).toBeDefined());
    expect(requestSimulation).toHaveBeenCalledTimes(1);
    expect(vi.mocked(requestSimulation).mock.calls[0][0]).toMatchObject({
      simStart: contextFixture.simStart,
      simEnd: contextFixture.simEnd,
      isStart: contextFixture.isStart,
      osStart: contextFixture.osStart,
      universe: contextFixture.universe,
      threads: contextFixture.threads,
      executionPrice: contextFixture.executionPrice,
      output: 'performance',
      xml: candidateFixture(0).xml,
    });
    act(() => useRunStore.getState().setSettings({ universe: 'TOP3000', threads: 1 }));
    expect(requestSimulation).toHaveBeenCalledTimes(1);
  });
  it('loads signals only in Signals view and reuses them on return', async () => {
    useRunStore.setState({
      result: result({
        ...candidateFixture(0),
        performance: singleResponseFixture.alphas[0].performance,
      }),
      viewMode: 'detail',
      selectedAlphaIndex: 0,
      selectedSignalAlphaIndex: 0,
    });
    vi.mocked(requestSimulation).mockResolvedValue(singleResponseFixture);
    renderHook(() => useResultData());
    expect(requestSimulation).not.toHaveBeenCalled();
    act(() => useRunStore.getState().exitDetailView());
    await waitFor(() =>
      expect(useRunStore.getState().result?.alphas[0].signals?.plot).toBeDefined(),
    );
    expect(vi.mocked(requestSimulation).mock.calls[0][0].output).toBe('signals');
    act(() => useRunStore.getState().enterDetailView(0));
    act(() => useRunStore.getState().exitDetailView());
    expect(
      vi.mocked(requestSimulation).mock.calls.filter(([r]) => r.output === 'signals'),
    ).toHaveLength(1);
  });
  it('rejects a stale signal response after a new run', async () => {
    const pending = deferred<SimulationResponse>();
    vi.mocked(requestSimulation).mockReturnValue(pending.promise);
    useRunStore.setState({ result: result(), viewMode: 'single', selectedSignalAlphaIndex: 0 });
    renderHook(() => useResultData());
    await waitFor(() => expect(requestSimulation).toHaveBeenCalledOnce());
    const signal = vi.mocked(requestSimulation).mock.calls[0][1];
    act(() => useRunStore.getState().beginRun(contextFixture, '<new/>'));
    await act(async () => pending.resolve(singleResponseFixture));
    expect(signal?.aborted).toBe(true);
    expect(useRunStore.getState().result?.alphas).toEqual([]);
  });
  it('reads saved performance while refusing live regeneration under an unavailable saved universe', async () => {
    const context = { ...contextFixture, universe: 'OLD' };
    useRunStore.setState({
      result: { ...result(), context, savedSource: 'saved' },
      viewMode: 'detail',
      selectedAlphaIndex: 0,
      selectedSignalAlphaIndex: 0,
    });
    vi.mocked(getSavedRow).mockResolvedValue({ ...singleResponseFixture, context });
    renderHook(() => useResultData());
    await waitFor(() =>
      expect(getSavedRow).toHaveBeenCalledWith('saved', 0, expect.any(AbortSignal)),
    );
    await waitFor(() => expect(useRunStore.getState().result?.alphas[0].performance).toBeDefined());
    act(() => useRunStore.getState().exitDetailView());
    await waitFor(() => expect(useRunStore.getState().signalRequest.status).toBe('error'));
    expect(requestSimulation).not.toHaveBeenCalled();
    expect(useRunStore.getState().result?.context.universe).toBe('OLD');
  });
  it('uses the same stable-member combined cache across Stats and round tabs', async () => {
    useRunStore.getState().beginRun(contextFixture, '<source/>');
    useRunStore.getState().receiveSearchEvents([
      { type: 'round_start', round: 1, size: 2 },
      { type: 'candidate', candidate: candidateFixture(7) },
      { type: 'candidate', candidate: candidateFixture(2) },
      { type: 'round_end', round: 1, bestSharpe: 1 },
      { type: 'search_complete', stopReason: 'exhausted', evaluated: 2, bestXml: '<x/>' },
    ]);
    vi.mocked(requestSimulation).mockResolvedValue(multiResponseFixture);
    renderHook(() => useResultData());
    await waitFor(() => expect(useRunStore.getState().result?.combined?.performance).toBeDefined());
    const combinedCalls = () =>
      vi.mocked(requestSimulation).mock.calls.filter(([r]) => r.combinedOnly);
    expect(combinedCalls()).toHaveLength(1);
    expect(combinedCalls()[0][0].xmls).toEqual([candidateFixture(2).xml, candidateFixture(7).xml]);
    act(() => useRunStore.getState().selectResultTab(1));
    expect(combinedCalls()).toHaveLength(1);
  });
  it('reports config failure and retries explicitly', async () => {
    useRunStore.setState({ config: null, configRequest: { status: 'idle' } });
    vi.mocked(getConfig)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(engineConfigFixture);
    renderHook(() => useResultData());
    await waitFor(() =>
      expect(useRunStore.getState().configRequest).toEqual({ status: 'error', error: 'offline' }),
    );
    act(() => useRunStore.getState().retryConfig());
    await waitFor(() => expect(useRunStore.getState().configRequest.status).toBe('ready'));
    expect(getConfig).toHaveBeenCalledTimes(2);
  });
});
