import { describe, expect, it } from 'vitest';
import { robustCrossSection } from '@/lib/heatmapStats';

describe('per-date cross-sectional heatmap normalization', () => {
  it('compares instruments within each date, independently of the price level on other dates', () => {
    const z = robustCrossSection([[10, 20, 30], [1000, 2000, 3000]], 3);
    expect(z[0][0]).toBeCloseTo(-1 / 1.4826);
    expect(z[0][1]).toBe(0);
    expect(z[0][2]).toBeCloseTo(1 / 1.4826);
    expect(Array.from(z[1])).toEqual(Array.from(z[0]));
    const extended = robustCrossSection([[10, 20, 30], [1000, 2000, 3000], [-1e10, 0, 1e10]], 3);
    expect(Array.from(extended[0])).toEqual(Array.from(z[0]));
  });

  it('colors a positive raw value below the same-date median negatively', () => {
    const z = robustCrossSection([[100, 200, 300]], 3);
    expect(z[0][0]).toBeCloseTo(-1 / 1.4826);
    expect(z[0][2]).toBeGreaterThan(0);
  });

  it('preserves missing values and treats equal or single finite values as neutral', () => {
    const z = robustCrossSection([[7, 7, 7], [null, NaN, Infinity], [5, undefined, null]], 3);
    expect(Array.from(z[0])).toEqual([0, 0, 0]);
    expect(Array.from(z[1]).every(Number.isNaN)).toBe(true);
    expect(z[2][0]).toBe(0);
    expect(z[2][1]).toBeNaN();
    expect(z[2][2]).toBeNaN();
    expect(robustCrossSection([], 3)).toEqual([]);
  });

  it('retains cross-sectional outliers when MAD is zero', () => {
    const z = robustCrossSection([[10, 10, 10, 10, 20], [100, 100, 100, 100, 90]], 5);
    expect(z[0][0]).toBe(0);
    expect(z[0][4]).toBeCloseTo(2.5);
    expect(z[1][4]).toBeCloseTo(-2.5);
  });

  it('uses the midpoint median for an even number of instruments', () => {
    const z = robustCrossSection([[10, 20, 30, 40]], 4);
    expect(z[0][1]).toBeCloseTo(-5 / (10 * 1.4826));
    expect(z[0][2]).toBeCloseTo(5 / (10 * 1.4826));
  });

  it('caches by payload identity and symbol count without mutating raw data', () => {
    const values = [[1, 101], [2, 102], [3, 103]];
    const original = structuredClone(values);
    const first = robustCrossSection(values, 2);
    expect(robustCrossSection(values, 2)).toBe(first);
    const narrower = robustCrossSection(values, 1);
    expect(narrower).not.toBe(first);
    expect(narrower[0]).toHaveLength(1);
    expect(narrower[0][0]).toBe(0);
    expect(robustCrossSection(structuredClone(values), 2)).not.toBe(first);
    expect(values).toEqual(original);
  });
});
