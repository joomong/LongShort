import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import SignalDistribution from '@/components/SignalDistribution';
import { useRunStore } from '@/runStore';
import { contextFixture, candidateFixture } from './metricFixtures';
import type { Distribution, SignalPayload } from '@/types';
const initial = useRunStore.getState();
const linear: Distribution = { bins: [1, 2, 3], counts: [2, 4, 1], min: 1, max: 3 };
const logarithmic: Distribution = {
  bins: [1, 10, 100],
  counts: [3, 2, 1],
  min: 1,
  max: 100,
  scale: 'log',
  edges: [0.1, 3, 30, 300],
  totalCount: 6,
  linear,
  exampleSymbol: ['A', 'B', null],
  exampleTimestamp: [1000, 3000, null],
  exampleValue: [1, 10, null],
};
function seed(signals: SignalPayload) {
  useRunStore.setState({
    result: {
      context: contextFixture,
      sourceXml: '<x/>',
      alphas: [{ ...candidateFixture(0), signals }],
      combined: null,
    },
    selectedSignalAlphaIndex: 0,
    viewMode: 'single',
  });
}
beforeEach(() => useRunStore.setState(initial, true));
describe('engine-produced signal distributions', () => {
  it('switches scales without rebuilding statistics or changing source payload', () => {
    seed({ distribution: linear, logDistribution: logarithmic });
    const { container } = render(<SignalDistribution />);
    expect(container.querySelectorAll('[data-bin]')).toHaveLength(3);
    const before = useRunStore.getState().result;
    fireEvent.click(screen.getByRole('button', { name: 'Log' }));
    expect(container.querySelectorAll('[data-bin]')).toHaveLength(3);
    expect(useRunStore.getState().result).toBe(before);
  });
  it('selects engine-provided example cells in numeric time and retains raw histogram values', () => {
    seed({ distribution: linear, logDistribution: logarithmic });
    const { container } = render(<SignalDistribution />);
    fireEvent.click(screen.getByRole('button', { name: 'Log' }));
    const bins = container.querySelectorAll('[data-bin]');
    fireEvent.click(bins[1]);
    expect(useRunStore.getState()).toMatchObject({ selectedSymbol: 'B', selectedTimestamp: 3000 });
  });
  it('disables an unavailable log distribution', () => {
    seed({ distribution: linear });
    render(<SignalDistribution />);
    expect(screen.getByRole('button', { name: 'Log' })).toBeDisabled();
  });
});
