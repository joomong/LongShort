import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';

import { useAlphaStore } from '@/store/alphaStore';
import { useBacktest } from '@/hooks/useBacktest';
import { runGAStream } from '@/lib/api';
import { gaStreamEventsFixture, multiAlphaFixture } from './pipelineFixtures';

vi.mock('@/lib/api', () => ({
  runBacktest: vi.fn(),
  runGAStream: vi.fn(),
  getDataRange: vi.fn(),
  getSymbols: vi.fn(),
  saveAlpha: vi.fn(),
  getAlphaList: vi.fn(),
  getAlpha: vi.fn(),
}));

const initialState = useAlphaStore.getState();

beforeEach(() => {
  vi.clearAllMocks();
  useAlphaStore.setState(initialState, true);
});

function renderHookHarness() {
  let hookApi: ReturnType<typeof useBacktest> | null = null;

  function Harness() {
    hookApi = useBacktest();
    return null;
  }

  render(<Harness />);

  if (!hookApi) {
    throw new Error('useBacktest hook did not initialize');
  }
  return hookApi;
}

function parseNdjsonEvents() {
  return gaStreamEventsFixture
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line));
}

describe('useBacktest pipeline behavior', () => {
  it('applies stream events to store state and auto-selects first heatmap cell', async () => {
    const events = parseNdjsonEvents();

    const finalWithHeatmap = {
      ...multiAlphaFixture,
      heatmap: {
        timestamps: ['2024-01-02T00:00:00+00:00'],
        symbols: ['AAA'],
        values: [[1.0]],
      },
    };

    vi.mocked(runGAStream).mockImplementation(async (_xml, _settings, _topN, onEvent) => {
      onEvent(events[0]);
      onEvent(events[1]);
      onEvent({ type: 'complete', data: finalWithHeatmap });
    });

    const transitions: Array<unknown> = [];
    const unsubscribe = useAlphaStore.subscribe((state, prev) => {
      if (state.result !== prev.result && state.result) {
        transitions.push(state.result);
      }
    });

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    unsubscribe();

    expect(runGAStream).toHaveBeenCalledTimes(1);
    expect(transitions.length).toBeGreaterThanOrEqual(2); // partial + complete

    const state = useAlphaStore.getState();
    expect(state.isRunning).toBe(false);
    expect(state.gaProgress).toBeNull();
    expect(state.backtestError).toBeNull();
    expect(state.result).toEqual(finalWithHeatmap);
    expect(state.selectedSymbol).toBe('AAA');
    expect(state.selectedTimestamp).toBe('2024-01-02T00:00:00+00:00');
  });

  it('handles stream error events and resets running state', async () => {
    vi.mocked(runGAStream).mockImplementation(async (_xml, _settings, _topN, onEvent) => {
      onEvent({ type: 'error', message: 'stream failed' });
    });

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    const state = useAlphaStore.getState();
    expect(state.backtestError).toBe('stream failed');
    expect(state.isRunning).toBe(false);
    expect(state.gaProgress).toBeNull();
  });

  it('handles thrown transport errors from runGAStream', async () => {
    vi.mocked(runGAStream).mockRejectedValue(new Error('network down'));

    const hook = renderHookHarness();

    await act(async () => {
      await hook.run();
    });

    const state = useAlphaStore.getState();
    expect(state.backtestError).toContain('network down');
    expect(state.isRunning).toBe(false);
    expect(state.gaProgress).toBeNull();
  });
});
