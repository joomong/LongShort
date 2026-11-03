export const MILLION_DOLLARS = 1_000_000;
export function formatDate(epochMs: number): string {
  return Number.isFinite(epochMs) ? new Date(epochMs).toISOString().slice(0, 10) : '';
}
export function formatNumber(value: number | null | undefined, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}
export function formatPercent(value: number | null | undefined, digits = 2, signed = false): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${signed && value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%` : '—';
}
export function formatSignalTooltip(value: number): string {
  const abs = Math.abs(value);
  return abs >= 1000 ? value.toFixed(1) : abs >= 1 ? value.toFixed(3) : value.toFixed(6);
}
export function formatHistogramValue(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  const abs = Math.abs(value);
  if (abs === 0) return '0';
  if (abs >= 1e21) return BigInt(Math.round(value)).toString();
  if (abs >= 1000) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(2);
  const rounded = Number(abs.toPrecision(3));
  if (rounded >= 0.001) return value.toFixed(4);
  const text = value.toFixed(Math.min(20, 2 - Math.floor(Math.log10(rounded))));
  return Number(text) === 0 ? '0' : text;
}
/** Display label for a panel symbol: "TICKER (id)" when the server supplied a label, else the id. */
export function symbolLabel(symbol: string | null | undefined, labels?: Record<string, string> | null): string {
  if (!symbol) return '';
  const label = labels?.[symbol];
  return label && label !== symbol ? `${label} (${symbol})` : symbol;
}

export function symbolMatches(symbol: string, query: string, labels?: Record<string, string> | null): boolean {
  const q = query.toLowerCase();
  return symbol.toLowerCase().includes(q) || (labels?.[symbol]?.toLowerCase().includes(q) ?? false);
}
