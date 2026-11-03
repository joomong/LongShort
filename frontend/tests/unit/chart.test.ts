import { describe, expect, it } from 'vitest';
import { alignedSeries } from '@/chart';
import { formatDate, formatHistogramValue, formatPercent, formatSignalTooltip } from '@/format';
describe('display formatting and aligned engine data', () => {
  it('converts the common epoch-ms axis once and preserves null gaps', () => {
    const timestamps = [1704067200000, 1704153600000, 1704240000000];
    const values = [1, null, 3];
    const result = alignedSeries(timestamps, [values]);
    expect(result[0]).toEqual([1704067200, 1704153600, 1704240000]);
    expect(result[1]).toBe(values);
  });
  it('formats calendar dates in UTC and fractional percentages at the display boundary', () => {
    expect(formatDate(Date.UTC(2024, 0, 1))).toBe('2024-01-01');
    expect(formatPercent(0.267)).toBe('26.70%');
    expect(formatPercent(-0.05)).toBe('-5.00%');
  });
  it('retains distinct signal tooltip and histogram label precision', () => {
    expect(formatSignalTooltip(0.123456)).toBe('0.123456');
    expect(formatSignalTooltip(1234)).toBe('1234.0');
    expect(formatHistogramValue(0.123456)).not.toBe(formatSignalTooltip(0.123456));
  });
});
