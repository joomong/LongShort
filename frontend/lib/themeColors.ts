export type Theme = 'light' | 'dark';

const palettes = {
  light: {
    backgroundElevated: '#e5e7eb', chartBackground: '#ffffff', chartGrid: '#e5e7eb',
    chartText: '#4b5563', chartLine: '#f0a52b', accent: '#d88a13',
    positive: '#22c55e', negative: '#ef4444', neutral: '#9ca3af',
  },
  dark: {
    backgroundElevated: '#1a1a1a', chartBackground: '#000000', chartGrid: '#222222',
    chartText: '#aaaaaa', chartLine: '#0078ff', accent: '#0078ff',
    positive: '#4af6c3', negative: '#ff433d', neutral: '#666666',
  },
};
export type ThemeColors = typeof palettes.light;
const variables: Record<keyof ThemeColors, string> = {
  backgroundElevated: '--background-elevated', chartBackground: '--chart-background',
  chartGrid: '--chart-grid', chartText: '--chart-text', chartLine: '--chart-line',
  accent: '--accent', positive: '--metric-positive', negative: '--metric-negative',
  neutral: '--foreground-muted',
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
