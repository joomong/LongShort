import { getThemeColors, type ThemeColors } from './themeColors';

/** Positive raw signals are long, negative raw signals are short. */
export function signalToColor(value: number | null | undefined, min = -1, max = 1, colors: ThemeColors = getThemeColors()): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return colors.chartBackground;
  if (value === 0) return colors.neutral;
  const extent = Math.max(Math.abs(min), Math.abs(max));
  const strength = extent > 0 && Number.isFinite(extent) ? Math.min(1, Math.abs(value) / extent) : 1;
  const color = value > 0 ? colors.positive : colors.negative;
  // Canvas supports CSS colors; alpha expresses magnitude without changing sign.
  const hex = /^#([a-f\d]{6})$/i.exec(color);
  if (!hex) return color;
  const rgb = [0, 2, 4].map(offset => parseInt(hex[1].slice(offset, offset + 2), 16));
  return `rgba(${rgb.join(', ')}, ${0.25 + 0.75 * strength})`;
}
