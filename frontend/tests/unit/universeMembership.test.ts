import { describe, expect, it } from 'vitest';
import { isMember, splitByMembership } from '@/chart';
describe('membership display', () => {
  it('uses explicit universe membership and treats absent membership as unknown', () => {
    expect(isMember(['10'], 0, 0)).toBe(true);
    expect(isMember(['10'], 0, 1)).toBe(false);
    expect(isMember(undefined, 5, 0)).toBe(true);
  });
  it('joins out-of-universe segments to neighboring in-universe samples without inventing values', () => {
    const split = splitByMembership([1, 2, null, 4, 5], [true, false, false, true, true]);
    expect(split.inside).toEqual([1, null, null, 4, 5]);
    expect(split.outside).toEqual([1, 2, null, 4, null]);
  });
});
