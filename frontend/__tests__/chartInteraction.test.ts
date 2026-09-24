import { describe, expect, it } from 'vitest';
import type uPlot from 'uplot';
import { clampRange, wheelPixels } from '@/lib/chartInteraction';
import { robustCrossSection } from '@/lib/heatmapStats';
import { dateStrToUnix, timeAxisValues } from '@/lib/uplotUtils';

describe('chart navigation boundaries', () => {
  it('preserves zoom width at either boundary and handles empty/single dates', () => {
    expect(clampRange(-5, 5, 0, 20)).toEqual({ min: 0, max: 10 });
    expect(clampRange(15, 25, 0, 20)).toEqual({ min: 10, max: 20 });
    expect(clampRange(-50, 50, 0, 20)).toEqual({ min: 0, max: 20 });
    expect(clampRange(0, 1, 4, 4)).toBeNull();
    expect(clampRange(0, 1, NaN, NaN)).toBeNull();
  });
  it('normalizes pixel, line and page wheel units', () => {
    expect(wheelPixels(1, 0, 300)).toBe(1);
    expect(wheelPixels(1, 1, 300)).toBe(16);
    expect(wheelPixels(1, 2, 300)).toBe(300);
  });
  it('formats days, months and years according to visible range', () => {
    const stamp = dateStrToUnix('2025-09-03');
    const labels = (days: number) => timeAxisValues({ scales: { x: { min: stamp, max: stamp + days * 86400 } } } as unknown as uPlot, [stamp]);
    expect(labels(30)).toEqual(['2025-09-03']);
    expect(labels(365)).toEqual(['2025-09']);
    expect(labels(3650)).toEqual(['2025']);
  });
});

it('reuses heatmap statistics on repaint, invalidates on new data and preserves missing values', () => {
  const data = [[1, 2, 4, null], [null, null, null, null]];
  const first = robustCrossSection(data, 4);
  expect(robustCrossSection(data, 4)).toBe(first);
  expect(first[0][0]).toBeCloseTo(-1 / 1.4826);
  expect(first[0][3]).toBeNaN();
  expect(first[1][0]).toBeNaN();
  expect(robustCrossSection(structuredClone(data), 4)).not.toBe(first);
});
