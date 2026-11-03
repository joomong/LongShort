import React from 'react';
import type uPlot from 'uplot';
import { act, render } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SignalChart from '@/components/SignalChart';
import { useUPlot } from '@/chart';
import { useRunStore } from '@/runStore';
import { candidateFixture, contextFixture } from './metricFixtures';
vi.mock('@/chart', async (original) => ({
  ...(await original<typeof import('@/chart')>()),
  useContainerSize: () => ({ width: 600, height: 300 }),
  useUPlot: vi.fn(() => ({ current: null })),
}));
const initial = useRunStore.getState();
beforeEach(() => {
  useRunStore.setState(
    {
      ...initial,
      viewMode: 'single',
      selectedSignalAlphaIndex: 0,
      selectedSymbol: 'A',
      result: {
        context: contextFixture,
        sourceXml: '<x/>',
        combined: null,
        alphas: [
          {
            ...candidateFixture(0),
            signals: {
              plot: {
                timestamps: [1000, 2000],
                symbols: ['A'],
                values: [[1], [2]],
                centers: [1, 2],
                scales: [0, 0],
                rowStride: 1,
                sampled: false,
              },
            },
          },
        ],
      },
    },
    true,
  );
  vi.mocked(useUPlot).mockClear();
});
it('snapshot label edits preserve prepared arrays and plot options; structural changes update them', () => {
  useRunStore
    .getState()
    .addSnapshot({
      id: 'p',
      name: 'before',
      color: '#ff0000',
      signalSeries: { timestamps: [1000, 2000], values: [3, 4] },
    });
  render(<SignalChart />);
  const first = vi.mocked(useUPlot).mock.calls.at(-1)!;
  act(() => useRunStore.getState().updateSnapshotName('p', 'after'));
  const renamed = vi.mocked(useUPlot).mock.calls.at(-1)!;
  expect(renamed[1]).toBe(first[1]);
  expect(renamed[2]).toBe(first[2]);
  const delayedPlot = { series: [{}, {}, {}, { label: 'before' }] } as unknown as uPlot;
  first[1]!.hooks!.ready!.forEach(ready => ready?.(delayedPlot));
  expect(delayedPlot.series[3].label).toBe('after');
  act(() => useRunStore.getState().removeSnapshot('p'));
  const removed = vi.mocked(useUPlot).mock.calls.at(-1)!;
  expect(removed[1]).not.toBe(first[1]);
  expect(removed[2]).not.toBe(first[2]);
});
