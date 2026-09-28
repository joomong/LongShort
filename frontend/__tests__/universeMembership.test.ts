import { describe, expect, it } from 'vitest';
import { isMember, splitByMembership } from '@/lib/universeMembership';

describe('universe membership helpers', () => {
  it('reads the engine row strings and treats missing membership as in-universe', () => {
    const m = ['10', '01'];
    expect(isMember(m, 0, 0)).toBe(true);
    expect(isMember(m, 0, 1)).toBe(false);
    expect(isMember(m, 1, 0)).toBe(false);
    expect(isMember(undefined, 0, 0)).toBe(true);
    expect(isMember(m, 5, 0)).toBe(true);
  });
  it('splits a series into a normal part and a grey out-of-universe part joined at the edges', () => {
    const { inside, outside } = splitByMembership([1, 2, 3, 4, 5], [true, true, false, false, true]);
    expect(inside).toEqual([1, 2, null, null, 5]);
    expect(outside).toEqual([null, 2, 3, 4, 5]);
  });
});
