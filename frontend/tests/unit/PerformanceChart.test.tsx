import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PerformanceChart from '@/components/PerformanceChart';
import { useUPlot } from '@/chart';
import { useRunStore } from '@/runStore';
import { candidateFixture, contextFixture } from './metricFixtures';
import type { PerformanceSeries } from '@/types';
vi.mock('@/chart', async (original) => ({
  ...(await original<typeof import('@/chart')>()),
  useContainerSize: () => ({ width: 600, height: 300 }),
  useUPlot: vi.fn(() => ({ current: null })),
}));
const initial = useRunStore.getState();
const series: PerformanceSeries = {
  timestamps: [1000, 2000, 3000],
  cumulativeNet: [-0.1, 0.2, 0.1],
  cumulativeGross: [0, 0.3, 0.2],
  drawdown: [0, 0, -0.1],
  turnover: [0.1, 0.2, 0.3],
  maxWeight: [0.2, 0.3, 0.4],
  positionCount: [2, 3, 4],
  osStartIndex: 2,
};
beforeEach(() => {
  useRunStore.setState({ ...initial, viewMode: 'detail' }, true);
  vi.mocked(useUPlot).mockClear();
});
describe('engine-ready performance chart', () => {
  it('passes authoritative net/gross arrays directly, retaining one shared time axis', () => {
    render(
      <PerformanceChart
        alpha={{ ...candidateFixture(0), performance: series }}
        metric="pnl"
        context={contextFixture}
      />,
    );
    const call = vi.mocked(useUPlot).mock.calls.at(-1)!;
    expect(call[2][0]).toEqual([1, 2, 3]);
    expect(call[2][1]).toBe(series.cumulativeNet);
    expect(call[2][2]).toBe(series.cumulativeGross);
    expect(screen.getByText('━ Gross', { exact: true })).toBeInTheDocument();
    expect(screen.getByText('┄ Net', { exact: true })).toBeInTheDocument();
  });
  it.each([
    ['mdd', 'drawdown'],
    ['turnover', 'turnover'],
    ['maxWeight', 'maxWeight'],
    ['nonZero', 'positionCount'],
  ] as const)('consumes %s without rebuilding financial series', (metric, key) => {
    render(
      <PerformanceChart
        alpha={{ ...candidateFixture(0), performance: series }}
        metric={metric}
        context={contextFixture}
      />,
    );
    expect(vi.mocked(useUPlot).mock.calls.at(-1)![2][1]).toBe(series[key]);
  });
  it('disables hidden chart data updates while preserving its lifecycle key', () => {
    render(
      <PerformanceChart
        alpha={{ ...candidateFixture(7), performance: series }}
        metric="pnl"
        context={contextFixture}
      />,
    );
    const key = vi.mocked(useUPlot).mock.calls.at(-1)![4];
    act(() => useRunStore.setState({ viewMode: 'single' }));
    const call = vi.mocked(useUPlot).mock.calls.at(-1)!;
    expect(call[5]).toBe(false);
    expect(call[4]).toBe(key);
    expect(call[2]).toEqual([[], []]);
  });
});
