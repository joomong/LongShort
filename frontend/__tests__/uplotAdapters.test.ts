import { describe, it, expect } from 'vitest';
import {
  dateStrToUnix,
  toAlignedData,
  toMultiSeriesData,
  toIsOsAlignedData,
  fmtMonth,
  fmtDate,
} from '@/lib/uplotUtils';

describe('uplotAdapters', () => {
  it('dateStrToUnix converts YYYY-MM-DD to unix seconds in UTC', () => {
    expect(dateStrToUnix('2024-01-01')).toBe(1704067200);
    expect(dateStrToUnix('2024-06-15')).toBe(1718409600);
  });

  it('toAlignedData converts {date, value}[] to uPlot format', () => {
    const points = [
      { date: '2024-01-01', value: 1.5 },
      { date: '2024-01-02', value: null },
      { date: '2024-01-03', value: 3.0 },
    ];
    const [ts, vals] = toAlignedData(points);
    expect(ts).toHaveLength(3);
    expect(ts[0]).toBe(dateStrToUnix('2024-01-01'));
    expect(vals).toEqual([1.5, null, 3.0]);
  });

  it('toMultiSeriesData converts record array to multi-series uPlot data', () => {
    const points = [
      { date: '2024-01-01', a: 1.0, b: 2.0 },
      { date: '2024-01-02', a: null, b: 3.0 },
      { date: '2024-01-03', a: 4.0, b: undefined },
    ];
    const result = toMultiSeriesData(
      points as Record<string, unknown>[],
      'date',
      ['a', 'b']
    );
    expect(result).toHaveLength(3); // [timestamps, seriesA, seriesB]
    expect(result[1]).toEqual([1.0, null, 4.0]);
    expect(result[2]).toEqual([2.0, 3.0, null]);
  });

  it('toIsOsAlignedData converts IS/OS split data', () => {
    const data = [
      { date: '2024-01-01', isNet: 100 },
      { date: '2024-01-02', isNet: 200, osNet: 200 },
      { date: '2024-01-03', osNet: 150 },
    ];
    const [ts, isVals, osVals] = toIsOsAlignedData(data);
    expect(ts).toHaveLength(3);
    expect(isVals).toEqual([100, 200, null]);
    expect(osVals).toEqual([null, 200, 150]);
  });

  it('fmtMonth formats unix seconds to YYYY-MM', () => {
    expect(fmtMonth(dateStrToUnix('2024-03-15'))).toBe('2024-03');
  });

  it('fmtDate formats unix seconds to YYYY-MM-DD', () => {
    expect(fmtDate(dateStrToUnix('2024-03-15'))).toBe('2024-03-15');
  });
});
