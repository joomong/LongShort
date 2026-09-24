import { getThemeColors, type ThemeColors } from './themeColors';

/**
 * Diverging colour for a normalized z-score: positive = red, negative = blue,
 * |z| = 0 is the chart background and the colour saturates at |z| >= Z_CLIP.
 * Magnitude is encoded as colour depth (a blend toward the pure hue), not alpha.
 */
export const Z_CLIP = 3;
const RED: [number, number, number] = [214, 39, 40];
const BLUE: [number, number, number] = [31, 90, 214];

function parseHex(color: string): [number, number, number] | null {
  const hex = /^#([a-f\d]{6})$/i.exec(color);
  if (!hex) return null;
  return [0, 2, 4].map(offset => parseInt(hex[1].slice(offset, offset + 2), 16)) as [number, number, number];
}

export function zscoreToColor(z: number | null | undefined, colors: ThemeColors = getThemeColors()): string {
  if (typeof z !== 'number' || !Number.isFinite(z)) return colors.chartBackground;
  const base = parseHex(colors.chartBackground) ?? [255, 255, 255];
  const t = Math.min(1, Math.abs(z) / Z_CLIP);
  if (t === 0) return colors.chartBackground;
  const hue = z > 0 ? RED : BLUE;
  const rgb = base.map((b, i) => Math.round(b + (hue[i] - b) * t));
  return `rgb(${rgb.join(', ')})`;
}

/** RGB triple for direct ImageData writes (same mapping as zscoreToColor). */
export function zscoreToRgb(z: number, base: [number, number, number]): [number, number, number] {
  const t = Math.min(1, Math.abs(z) / Z_CLIP);
  const hue = z > 0 ? RED : BLUE;
  return [0, 1, 2].map(i => Math.round(base[i] + (hue[i] - base[i]) * t)) as [number, number, number];
}

/** Legacy raw-value mapping kept for callers outside the heatmap: sign + magnitude relative to [min,max]. */
export function signalToColor(value: number | null | undefined, min = -1, max = 1, colors: ThemeColors = getThemeColors()): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return colors.chartBackground;
  if (value === 0) return colors.neutral;
  const extent = Math.max(Math.abs(min), Math.abs(max));
  const strength = extent > 0 && Number.isFinite(extent) ? Math.min(1, Math.abs(value) / extent) : 1;
  return zscoreToColor(Math.sign(value) * strength * Z_CLIP, colors);
}

export { parseHex };
