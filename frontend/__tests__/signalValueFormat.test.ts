import { describe, expect, it } from 'vitest';
import { formatSignalValue } from '@/components/visualization/SignalDistribution';

describe('formatSignalValue', () => {
  it.each([
    [1.23e-4, '0.000123'], [-1.23e-4, '-0.000123'], [5e-7, '0.000000500'], [-5e-7, '-0.000000500'],
    [9.99e-4, '0.000999'], [0.001, '0.0010'], [0, '0'], [-0, '0'], [1e-25, '0'], [12345.6, '12346'],
    [1.5, '1.50'], [0.5, '0.5000'], [1e21, '1000000000000000000000'], [-1.5e21, '-1500000000000000000000'],
    [1e300, BigInt(1e300).toString()], [0.00099995, '0.0010'], [9.9995e-5, '0.000100'], [Infinity, 'Infinity'], [NaN, 'NaN'],
  ])('%s -> %s', (v, text) => {
    expect(formatSignalValue(v)).toBe(text);
    expect(formatSignalValue(v)).not.toMatch(/e/);
  });
});
