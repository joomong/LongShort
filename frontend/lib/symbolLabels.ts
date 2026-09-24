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
