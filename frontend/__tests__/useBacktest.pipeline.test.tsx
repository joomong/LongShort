import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';
import { useAlphaStore } from '@/store/alphaStore';
import { useBacktest } from '@/hooks/useBacktest';
import { runGAStream, adaptGACandidate } from '@/lib/api';
import type { StarGAEvent } from '@/types';
import { multiAlphaMetricFixture } from './metricFixtures';

vi.mock('@/lib/api', () => ({
  runGAStream: vi.fn(), adaptGACompleteToResponse: vi.fn(), adaptGACandidate: vi.fn(), getConfig: vi.fn(),
}));
const initial = useAlphaStore.getState();
beforeEach(() => { vi.clearAllMocks(); useAlphaStore.setState(initial, true); });
function harness() {
  let hook!: ReturnType<typeof useBacktest>;
  function Harness() { hook = useBacktest(); return null; }
  render(<Harness />); return hook;
}
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
const complete = { type: 'search_complete', bestXml: '<data/>', stopReason: 'exhausted', evaluated: 0 } as StarGAEvent;

describe('default GA run', () => {
  it('runs GA with the source XML and search budgets instead of ordinary simulation', async () => {
    const xml = '<data expr="-returns"/><rank/><tsmean days="5|20"/><neutralize group="sector|industry"/>';
    useAlphaStore.getState().setXmlCode(xml);
    vi.mocked(runGAStream).mockImplementation(async (...args) => { args[7](complete); });
    const hook = harness();
    await act(async () => { await hook.run(); });
    expect(runGAStream).toHaveBeenCalledTimes(1);
    const args = vi.mocked(runGAStream).mock.calls[0];
    expect(args[0]).toBe(xml); expect(args.slice(4, 7)).toEqual([200, 40, 20]);
    expect(args[8]).toBeInstanceOf(AbortSignal);
    expect(args[12]).toEqual({ stopAlphas: 400, roundSize: 50 });
    expect(useAlphaStore.getState()).toMatchObject({ gaSession: true, isRunning: false, xmlCode: xml, backtestError: null });
  });
  it('streams candidates into live rounds and preserves them in Stats after completion', async () => {
    const candidate = { ...multiAlphaMetricFixture.alphas[0], candidateId: 0, round: 1, xml: '<data/>' };
    vi.mocked(adaptGACandidate).mockReturnValue(candidate);
    vi.mocked(runGAStream).mockImplementation(async (...args) => {
      args[7]({ type: 'round_start', round: 1, size: 1 });
      args[7]({ type: 'candidate', candidate: { index: 0, round: 1, xml: '<data/>' } } as StarGAEvent);
      expect(useAlphaStore.getState().result?.alphas).toEqual([candidate]);
      expect(useAlphaStore.getState().isRunning).toBe(true);
      args[7]({ type: 'round_end', round: 1, bestXml: '<data/>', bestSharpe: 1 } as StarGAEvent);
      args[7](complete);
    });
    const hook = harness();
    await act(async () => { await hook.run(); });
    expect(useAlphaStore.getState()).toMatchObject({ activeResultTab: 'stats', gaRounds: [{ round: 1, status: 'completed' }], gaProgress: null });
    expect(useAlphaStore.getState().result?.alphas).toEqual([candidate]);
  });
  it('reports transport failures and clears the running state', async () => {
    vi.mocked(runGAStream).mockRejectedValue(new Error('network down'));
    const hook = harness();
    await act(async () => { await hook.run(); });
    expect(useAlphaStore.getState()).toMatchObject({ backtestError: 'network down', isRunning: false, gaProgress: null });
  });
  it('ignores events from an earlier run when another run has started', async () => {
    const pending = deferred(); let old!: (event: StarGAEvent) => void;
    vi.mocked(runGAStream).mockImplementationOnce(async (...args) => { old = args[7]; await pending.promise; })
      .mockImplementationOnce(async (...args) => { args[7](complete); });
    const hook = harness(); let first!: Promise<void>;
    act(() => { first = hook.run(); });
    const signal = vi.mocked(runGAStream).mock.calls[0][8];
    await act(async () => { await hook.run(); });
    act(() => old({ type: 'error', error: 'obsolete error' }));
    await act(async () => { pending.resolve(); await first; });
    expect(signal?.aborted).toBe(true);
    expect(useAlphaStore.getState().backtestError).toBeNull();
  });
  it('forwards thread count, universe and execution price and snapshots run settings', async () => {
    useAlphaStore.getState().setSettings({ nJobs: 3, universe: 'TOP1000', executionPrice: 'vwap' });
    useAlphaStore.setState({ config: { executionPrices: ['close', 'vwap'], supportsSimEnd: true, simEnd: '2025-12-31' } as never });
    vi.mocked(runGAStream).mockImplementation(async (...args) => { args[7](complete); });
    const hook = harness();
    await act(async () => { await hook.run(); });
    const args = vi.mocked(runGAStream).mock.calls[0];
    expect(args[6]).toBe(3); expect(args[9]).toBe('TOP1000'); expect(args[10]).toBe('vwap');
    expect(useAlphaStore.getState().result?.runSettings).toMatchObject({ threads: 3, universe: 'TOP1000', executionPrice: 'vwap' });
  });
  it('Stop aborts GA and ignores late events without losing the source template', async () => {
    const pending = deferred(); let observer!: (event: StarGAEvent) => void;
    vi.mocked(runGAStream).mockImplementation(async (...args) => { observer = args[7]; await pending.promise; });
    const xml = '<data expr="close" delay="2"/>'; useAlphaStore.getState().setXmlCode(xml);
    const hook = harness(); let running!: Promise<void>;
    act(() => { running = hook.run(); });
    const signal = vi.mocked(runGAStream).mock.calls[0][8];
    act(() => { hook.stop(); observer({ type: 'error', error: 'late' }); });
    await act(async () => { pending.resolve(); await running; });
    expect(signal?.aborted).toBe(true);
    expect(useAlphaStore.getState()).toMatchObject({ xmlCode: xml, isRunning: false, gaProgress: null, backtestError: null });
  });
});
