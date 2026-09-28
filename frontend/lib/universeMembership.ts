// Signal-date universe membership sent by the engine with the signal plot
// (`membership`: one '1'/'0' string per returned row, aligned with `values`).
// Values may already be NaN outside the universe, so membership is explicit.

/** Grey used for cells/segments where the symbol is outside the run's universe. */
export const OUT_OF_UNIVERSE_RGB: [number, number, number] = [160, 160, 160];
export const OUT_OF_UNIVERSE_HEX = '#a0a0a0';

/** false only when membership is present and marks the cell out; true when unknown. */
export function isMember(membership: string[] | undefined | null, row: number, col: number): boolean {
  const r = membership?.[row];
  return r === undefined || r[col] !== '0';
}

/**
 * Split one series into an in-universe and an out-of-universe series (null elsewhere).
 * The out series also keeps the in-universe neighbours of each out run so the grey
 * segment joins the normal line instead of leaving a gap.
 */
export function splitByMembership(values: (number | null)[], members: boolean[]) {
  const inside = values.map((v, i) => (members[i] === false ? null : v));
  const outside = values.map((v, i) => {
    const out = (k: number) => k >= 0 && k < values.length && members[k] === false;
    return out(i) || out(i - 1) || out(i + 1) ? v : null;
  });
  return { inside, outside };
}
