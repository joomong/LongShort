import { describe, expect, it } from 'vitest';
import {
  multiResponseFixture,
  searchEventsFixture,
  singleResponseFixture,
} from './pipelineFixtures';

describe('captured result contract', () => {
  it('has one canonical result owner and fractional summary units', () => {
    const alpha = singleResponseFixture.alphas[0];
    expect(alpha.summary?.is.avgPositionCount).toBe(6);
    expect(alpha.summary?.is.minPositionCount).toBe(6);
    expect(alpha.summary?.is.turnover).toBeCloseTo(0.09094122637952427, 14);
    expect(singleResponseFixture).not.toHaveProperty('timeseries');
    expect(singleResponseFixture).not.toHaveProperty('signalPlot');
    expect(singleResponseFixture).not.toHaveProperty('batches');
  });
  it('keeps engine-ready aligned report curves without needing daily-return reconstruction', () => {
    for (const alpha of [...multiResponseFixture.alphas, multiResponseFixture.combined!]) {
      const p = alpha.performance!;
      expect(p.timestamps.every(Number.isFinite)).toBe(true);
      expect(p.timestamps.length).toBe(p.cumulativeNet.length);
      expect(p.drawdown.length).toBe(p.timestamps.length);
      expect(p.osStartIndex).toBeGreaterThan(0);
      expect(p.osStartIndex).toBeLessThanOrEqual(p.timestamps.length);
      let peak = 0;
      for (let i = 0; i < p.drawdown.length; i++) {
        peak = Math.max(peak, p.cumulativeGross[i]);
        expect(p.drawdown[i]).toBeCloseTo(p.cumulativeGross[i] - peak, 14);
      }
    }
  });
  it('keeps distinct raw signals and supplies per-date normalization statistics', () => {
    const plot = multiResponseFixture.combined!.signals!.plot!;
    expect(plot.timestamps.length).toBe(plot.values.length);
    expect(plot.centers).toHaveLength(plot.values.length);
    expect(plot.scales).toHaveLength(plot.values.length);
    expect(plot.sampled).toBe(true);
    expect(plot.timestamps.at(-1)).toBe(Date.parse('2025-12-31'));
    expect(multiResponseFixture.alphas[0].signals?.plot).not.toEqual(plot);
  });
  it('uses only the candidate stream protocol', () => {
    expect(searchEventsFixture.map((e) => e.type)).toEqual([
      'search_start',
      'round_start',
      'candidate',
      'round_end',
      'search_complete',
    ]);
    const event = searchEventsFixture.find((e) => e.type === 'candidate');
    if (event?.type !== 'candidate') throw new Error('missing candidate');
    expect(event.candidate).not.toHaveProperty('performance');
    expect(event.candidate).not.toHaveProperty('signals');
    expect(event.candidate.id).toBe(0);
  });
});
