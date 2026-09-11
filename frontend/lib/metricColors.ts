export function getSignCellColor(value: number): string {
  if (!Number.isFinite(value)) return 'text-foreground-muted';
  if (value > 0) return 'text-metric-positive';
  if (value < 0) return 'text-metric-negative';
  return 'text-foreground-muted';
}

export function getMetricCellColor(metric: string, value: number): string {
  if (!Number.isFinite(value)) return 'text-foreground-muted';
  // Turnover is an activity measure, without an intrinsic profit/loss sign.
  if (metric === 'turnover') return 'text-data-color';
  return getSignCellColor(value);
}
