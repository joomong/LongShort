import { describe, expect, it } from 'vitest';
import { signalToColor, zscoreToColor } from '@/lib/colorScale';
import { getThemeColors } from '@/lib/themeColors';
import { getSignCellColor } from '@/lib/metricColors';

describe('signal side colors', () => {
  it('keeps every positive signal long even when it is the smallest positive value', () => {
    const colors = getThemeColors('dark');
    // positive = red hue blended from the chart background (black in dark theme)
    expect(signalToColor(1, 1, 100, colors)).toMatch(/^rgb\(\d+, 0, 0\)$/);
    expect(signalToColor(100, 1, 100, colors)).toBe('rgb(214, 39, 40)');
    expect(getSignCellColor(1)).toBe('text-metric-positive');
  });
  it('uses the short color for negatives and distinct neutral/missing states', () => {
    const colors = getThemeColors('dark');
    expect(signalToColor(-1, -100, -1, colors)).toMatch(/^rgb\(0, \d+, \d+\)$/);
    expect(zscoreToColor(-3, colors)).toBe('rgb(31, 90, 214)');
    expect(zscoreToColor(0, colors)).toBe(colors.chartBackground);
    expect(getSignCellColor(-1)).toBe('text-metric-negative');
    expect(signalToColor(0, -1, 1, colors)).toBe(colors.neutral);
    expect(signalToColor(NaN, -1, 1, colors)).toBe(colors.chartBackground);
  });
});
