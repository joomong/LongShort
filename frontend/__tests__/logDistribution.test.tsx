import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import SignalDistribution from '@/components/visualization/SignalDistribution';
import { adaptSimulateResponse } from '@/lib/api';
import { useAlphaStore } from '@/store/alphaStore';
import rawFixture from './fixtures/cpp_single.json';
import type { StarBatchResponse, StarDistribution } from '@/types';

const fixture: StarBatchResponse = { ...rawFixture, mode: rawFixture.mode as 0 | 1 };
const initial = useAlphaStore.getState();
const logarithmic: StarDistribution = {
  scale: 'symlog', min: -10, max: 10, bins: [-3, 0, 3], edges: [-10, -1, 1, 10], counts: [1, 8, 2],
  linear: { scale: 'linear', min: -10, max: 10, bins: [-5, 5], edges: [-10, 0, 10], counts: [1, 10] },
  exampleSymbol: ['AAA', 'BBB', 'CCC'], exampleDate: ['2025-09-03', '2025-09-17', '2025-10-01'],
};
beforeEach(() => useAlphaStore.setState(initial, true));

describe('log histogram display', () => {
  it('defaults to whole-population log bins, preserves zero/tails, toggles linear and selects the matching example', () => {
    const response: StarBatchResponse = { ...fixture, signalDistributionLog: logarithmic };
    useAlphaStore.getState().setResult(adaptSimulateResponse(response));
    const { container } = render(<SignalDistribution />);
    expect(screen.getByRole('button', { name: 'Log' })).toHaveAttribute('aria-pressed', 'true');
    const bars = () => Array.from(container.querySelectorAll('[data-bin]'));
    expect(bars().map(el => Number(el.getAttribute('data-count')))).toEqual([1, 8, 2]);
    expect(bars()[0]).toHaveAttribute('title', expect.stringContaining('-10.00 – -1.00'));
    fireEvent.click(bars()[2]);
    expect(useAlphaStore.getState()).toMatchObject({ selectedSymbol: 'CCC', selectedTimestamp: '2025-10-01' });
    fireEvent.click(screen.getByRole('button', { name: 'Linear' }));
    expect(bars().map(el => Number(el.getAttribute('data-count')))).toEqual([1, 10]);
    fireEvent.click(screen.getByRole('button', { name: 'Log' }));
    expect(bars()).toHaveLength(3);
  });
  it('does not present an older linear-only response as logarithmic', () => {
    useAlphaStore.getState().setResult(adaptSimulateResponse(fixture));
    render(<SignalDistribution />);
    expect(screen.getByRole('button', { name: 'Log' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Linear' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('histogram colour strip', () => {
  it('selects the bin example by clicking the strip cell under the bars; empty bins are inert', () => {
    const response: StarBatchResponse = { ...fixture, signalDistributionLog: logarithmic };
    useAlphaStore.getState().setResult(adaptSimulateResponse(response));
    const { container } = render(<SignalDistribution />);
    const strip = screen.getByLabelText('Distribution value scale');
    const cells = Array.from(strip.querySelectorAll('[data-strip-bin]'));
    expect(cells).toHaveLength(3);
    expect(container.querySelectorAll('[data-bin]')).toHaveLength(3); // bars unchanged
    fireEvent.click(cells[1]);
    expect(useAlphaStore.getState()).toMatchObject({ selectedSymbol: 'BBB', selectedTimestamp: '2025-09-17' });
    fireEvent.click(cells[0]);
    expect(useAlphaStore.getState()).toMatchObject({ selectedSymbol: 'AAA', selectedTimestamp: '2025-09-03' });
  });
});
