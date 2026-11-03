type Theme = 'light' | 'dark';

const palettes = {
  light: {
    chartBackground: '#ffffff', chartGrid: '#e5e7eb',
    chartText: '#4b5563', chartLine: '#f0a52b', accent: '#d88a13',
  },
  dark: {
    chartBackground: '#000000', chartGrid: '#222222',
    chartText: '#aaaaaa', chartLine: '#0078ff', accent: '#0078ff',
  },
};
export type ThemeColors = typeof palettes.light;
const variables: Record<keyof ThemeColors, string> = {
  chartBackground: '--chart-background',
  chartGrid: '--chart-grid', chartText: '--chart-text', chartLine: '--chart-line',
  accent: '--accent',
};

export function getThemeColors(theme?: Theme): ThemeColors {
  const actual = typeof document !== 'undefined' && document.documentElement.classList.contains('dark') ? 'dark' : 'light';
  const selected = theme ?? actual;
  const colors = { ...palettes[selected] };
  if (typeof document !== 'undefined' && selected === actual) {
    const style = getComputedStyle(document.documentElement);
    for (const key of Object.keys(variables) as (keyof ThemeColors)[]) {
      colors[key] = style.getPropertyValue(variables[key]).trim() || colors[key];
    }
  }
  return colors;
}

export const SIGNAL_DISTRIBUTION_HEIGHT = 64;
export const SIGNAL_HEATMAP_HEIGHT = SIGNAL_DISTRIBUTION_HEIGHT * 3;
export const OUT_OF_UNIVERSE_RGB: [number, number, number] = [160, 160, 160];
export const OUT_OF_UNIVERSE_HEX = '#a0a0a0';
const SNAPSHOT_COLORS = ['#ff9800', '#a78bfa', '#22d3ee', '#f472b6', '#a3e635', '#facc15', '#818cf8', '#fb923c'];

export function getNextColor(usedColors: readonly string[]): string {
  const used = new Set(usedColors.map(color => color.toLowerCase()));
  return SNAPSHOT_COLORS.find(color => !used.has(color.toLowerCase())) ?? SNAPSHOT_COLORS[usedColors.length % SNAPSHOT_COLORS.length];
}

export function getMetricCellColor(metric: string, value: number): string {
  if (!Number.isFinite(value)) return 'text-foreground-muted';
  if (metric === 'turnover') return 'text-data-color';
  return value > 0 ? 'text-metric-positive' : value < 0 ? 'text-metric-negative' : 'text-foreground-muted';
}

export function parseHex(color: string): [number, number, number] | null {
  const hex = /^#([a-f\d]{6})$/i.exec(color);
  if (!hex) return null;
  return [parseInt(hex[1].slice(0, 2), 16), parseInt(hex[1].slice(2, 4), 16), parseInt(hex[1].slice(4, 6), 16)];
}

/** Write one normalized color directly, without allocating per-pixel tuples. */
export function writeSignalPixel(pixels: Uint8ClampedArray, offset: number, z: number, base: readonly number[]) {
  const strength = Math.min(1, Math.abs(z) / 3);
  const red = z > 0 ? 214 : 31, green = z > 0 ? 39 : 90, blue = z > 0 ? 40 : 214;
  pixels[offset] = Math.round(base[0] + (red - base[0]) * strength);
  pixels[offset + 1] = Math.round(base[1] + (green - base[1]) * strength);
  pixels[offset + 2] = Math.round(base[2] + (blue - base[2]) * strength);
}
