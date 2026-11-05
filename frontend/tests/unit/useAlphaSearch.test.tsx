import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRunStore } from '@/runStore';
import { useAlphaSearch } from '@/useAlphaSearch';
import { streamAlphaSearch } from '@/api';
import type { SearchEvent } from '@/types';
import { candidateFixture, contextFixture, engineConfigFixture } from './metricFixtures';
vi.mock('@/api', () => ({ streamAlphaSearch: vi.fn() }));
const initial = useRunStore.getState();
const complete: SearchEvent = {
  type: 'search_complete',
  stopReason: 'exhausted',
  evaluated: 1,
  bestXml: '<x/>',
};
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  vi.clearAllMocks();
  useRunStore.setState(initial, true);
  useRunStore.getState().setConfig(engineConfigFixture);
});
describe('alpha search lifecycle', () => {
  it('snapshots current XML/settings into one native search request', async () => {
    const xml = '<data expr="close" delay="1"/><tsmean days="5|20"/>';
    useRunStore.getState().setXmlCode(xml);
    useRunStore
      .getState()
      .setSettings({
        threads: 3,
        universe: 'TOP500',
        executionPrice: 'vwap',
        firstSearch: 123,
        roundSize: 17,
        stopRound: 9,
        stopAlphas: 81,
      });
    vi.mocked(streamAlphaSearch).mockImplementation(async (_request, receive) => receive(complete));
    const { result } = renderHook(() => useAlphaSearch());
    await act(async () => result.current.startSearch());
    const [request, , signal] = vi.mocked(streamAlphaSearch).mock.calls[0];
    expect(request).toMatchObject({
      xml,
      threads: 3,
      universe: 'TOP500',
      executionPrice: 'vwap',
      firstSearch: 123,
      roundSize: 17,
      stopRound: 9,
      stopAlphas: 81,
    });
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(useRunStore.getState()).toMatchObject({
      isRunning: false,
      gaFinished: true,
      xmlCode: xml,
      backtestError: null,
    });
  });
  it('flushes every candidate before round and terminal transitions', async () => {
    vi.mocked(streamAlphaSearch).mockImplementation(async (_request, receive) => {
      receive({ type: 'search_start', runId: 'run-1', context: contextFixture });
      receive({ type: 'round_start', round: 1, size: 2 });
      receive({ type: 'candidate', candidate: candidateFixture(7) });
      receive({ type: 'candidate', candidate: candidateFixture(2) });
      receive({ type: 'round_end', round: 1, bestSharpe: 5 });
      receive({ ...complete, evaluated: 2 });
    });
    const { result } = renderHook(() => useAlphaSearch());
    await act(async () => result.current.startSearch());
    expect(useRunStore.getState().result?.alphas.map((a) => a.id)).toEqual([7, 2]);
    expect(useRunStore.getState().gaRounds).toEqual([{ round: 1, status: 'completed' }]);
    expect(useRunStore.getState().result?.context).toEqual(contextFixture);
  });
  it('ignores old events and aborts the earlier transport when a new run begins', async () => {
    const old = deferred();
    let receiveOld!: (event: SearchEvent) => void;
    vi.mocked(streamAlphaSearch)
      .mockImplementationOnce(async (_r, receive) => {
        receiveOld = receive;
        await old.promise;
      })
      .mockImplementationOnce(async (_r, receive) => receive(complete));
    const { result } = renderHook(() => useAlphaSearch());
    let first!: Promise<void>;
    act(() => {
      first = result.current.startSearch();
    });
    const signal = vi.mocked(streamAlphaSearch).mock.calls[0][2];
    await act(async () => result.current.startSearch());
    act(() => receiveOld({ type: 'error', error: 'obsolete' }));
    await act(async () => {
      old.resolve();
      await first;
    });
    expect(signal?.aborted).toBe(true);
    expect(useRunStore.getState().backtestError).toBeNull();
  });
  it('Stop flushes received rows, aborts the stream and ignores late events', async () => {
    const pending = deferred();
    let receive!: (event: SearchEvent) => void;
    vi.mocked(streamAlphaSearch).mockImplementation(async (_r, handler) => {
      receive = handler;
      handler({ type: 'round_start', round: 1, size: 2 });
      handler({ type: 'candidate', candidate: candidateFixture(5) });
      await pending.promise;
    });
    const { result } = renderHook(() => useAlphaSearch());
    let running!: Promise<void>;
    act(() => {
      running = result.current.startSearch();
    });
    const signal = vi.mocked(streamAlphaSearch).mock.calls[0][2];
    act(() => {
      result.current.stopSearch();
      receive({ type: 'error', error: 'late' });
    });
    await act(async () => {
      pending.resolve();
      await running;
    });
    expect(signal?.aborted).toBe(true);
    expect(useRunStore.getState().result?.alphas.map((a) => a.id)).toEqual([5]);
    expect(useRunStore.getState().backtestError).toBeNull();
  });
  it('reports transport failure and refuses to run before config is ready', async () => {
    vi.mocked(streamAlphaSearch).mockRejectedValue(new Error('network down'));
    const { result } = renderHook(() => useAlphaSearch());
    await act(async () => result.current.startSearch());
    expect(useRunStore.getState()).toMatchObject({
      isRunning: false,
      backtestError: 'network down',
    });
    vi.mocked(streamAlphaSearch).mockClear();
    useRunStore.setState({ config: null, configRequest: { status: 'error', error: 'offline' } });
    await act(async () => result.current.startSearch());
    expect(streamAlphaSearch).not.toHaveBeenCalled();
    expect(useRunStore.getState().backtestError).toContain('configuration');
  });
});
