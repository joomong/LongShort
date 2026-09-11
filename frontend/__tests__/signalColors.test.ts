import { describe, expect, it } from 'vitest';
import { signalToColor } from '@/lib/colorScale';
import { getThemeColors } from '@/lib/themeColors';
import { getSignCellColor } from '@/lib/metricColors';

describe('signal side colors', () => {
  it('keeps every positive signal long even when it is the smallest positive value', () => {
    const colors = getThemeColors('dark');
    expect(signalToColor(1, 1, 100, colors)).toMatch(/^rgba\(74, 246, 195,/);
    expect(signalToColor(100, 1, 100, colors)).toMatch(/^rgba\(74, 246, 195,/);
    expect(getSignCellColor(1)).toBe('text-metric-positive');
  });
  it('uses the short color for negatives and distinct neutral/missing states', () => {
    const colors = getThemeColors('dark');
    expect(signalToColor(-1, -100, -1, colors)).toMatch(/^rgba\(255, 67, 61,/);
    expect(getSignCellColor(-1)).toBe('text-metric-negative');
    expect(signalToColor(0, -1, 1, colors)).toBe(colors.neutral);
    expect(signalToColor(NaN, -1, 1, colors)).toBe(colors.chartBackground);
  });
});
