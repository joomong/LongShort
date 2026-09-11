export const SNAPSHOT_COLORS = ['#ff9800', '#a78bfa', '#22d3ee', '#f472b6', '#a3e635', '#facc15', '#818cf8', '#fb923c'];

export function getNextColor(usedColors: readonly string[]): string {
  const used = new Set(usedColors.map(color => color.toLowerCase()));
  return SNAPSHOT_COLORS.find(color => !used.has(color.toLowerCase())) ?? SNAPSHOT_COLORS[usedColors.length % SNAPSHOT_COLORS.length];
}
