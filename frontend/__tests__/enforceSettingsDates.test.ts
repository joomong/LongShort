import { describe, it, expect } from 'vitest';
import { enforceSettingsDates } from '@/store/alphaStore';
import type { BacktestSettings } from '@/types';

/** Helper to build a BacktestSettings object with date overrides. */
function makeSettings(overrides: Partial<BacktestSettings> = {}): BacktestSettings {
  return {
    isStart: '2023-01-01',
    isEnd: '2023-06-30',
    osStart: '2023-07-01',
    osEnd: '2023-12-31',
    barInterval: 'D1',
    nJobs: 5,
    gaPopulation: 50,
    ...overrides,
  };
}

describe('enforceSettingsDates', () => {
  it('1. Normal range - no changes needed', () => {
    const s = makeSettings();
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.isStart).toBe('2023-01-01');
    expect(r.isEnd).toBe('2023-06-30');
    expect(r.osStart).toBe('2023-07-01');
    expect(r.osEnd).toBe('2023-12-31');
  });

  it('2. isEnd >= osStart should push osStart forward via nextDay', () => {
    const s = makeSettings({ isEnd: '2023-07-01', osStart: '2023-07-01' });
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.isEnd).toBe('2023-07-01');
    expect(r.osStart).toBe('2023-07-02');
    expect(r.osStart > r.isEnd).toBe(true);
  });

  it('3. isStart > isEnd should adjust isEnd to match isStart', () => {
    const s = makeSettings({ isStart: '2023-08-01', isEnd: '2023-06-30' });
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.isEnd).toBe('2023-08-01');
    // osStart must still be > isEnd
    expect(r.osStart > r.isEnd).toBe(true);
  });

  it('4. osStart > osEnd should adjust osEnd to match osStart', () => {
    const s = makeSettings({ osStart: '2024-01-01', osEnd: '2023-06-30' });
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.osEnd).toBe(r.osStart);
    expect(r.osStart <= r.osEnd).toBe(true);
  });

  it('5. Clamping to minDate: isStart before minDate should be clamped', () => {
    const s = makeSettings({ isStart: '2019-01-01' });
    const r = enforceSettingsDates(s, '2022-01-01', '2025-12-31');
    expect(r.isStart).toBe('2022-01-01');
  });

  it('6. Clamping to maxDate: osEnd after maxDate should be clamped', () => {
    const s = makeSettings({ osEnd: '2030-01-01' });
    const r = enforceSettingsDates(s, '2020-01-01', '2024-06-30');
    expect(r.osEnd).toBe('2024-06-30');
  });

  it('7. All defaults outside data range (all before minDate) should clamp and enforce', () => {
    const s = makeSettings({
      isStart: '2019-01-01',
      isEnd: '2019-06-30',
      osStart: '2019-07-01',
      osEnd: '2019-12-31',
    });
    const r = enforceSettingsDates(s, '2023-01-01', '2025-12-31');
    // All dates clamped to minDate initially, then ordering enforced
    expect(r.isStart <= r.isEnd).toBe(true);
    expect(r.isEnd < r.osStart).toBe(true);
    expect(r.osStart <= r.osEnd).toBe(true);
    expect(r.isStart >= '2023-01-01').toBe(true);
    expect(r.osEnd <= '2025-12-31').toBe(true);
  });

  it('8. 2-day data range should produce valid IS=1day, OS=1day', () => {
    const s = makeSettings({
      isStart: '2023-06-01',
      isEnd: '2023-06-01',
      osStart: '2023-06-02',
      osEnd: '2023-06-02',
    });
    const r = enforceSettingsDates(s, '2023-06-01', '2023-06-02');
    expect(r.isStart).toBe('2023-06-01');
    expect(r.isEnd).toBe('2023-06-01');
    expect(r.osStart).toBe('2023-06-02');
    expect(r.osEnd).toBe('2023-06-02');
  });

  it('9. 3-day data range should work cleanly', () => {
    const s = makeSettings({
      isStart: '2023-06-01',
      isEnd: '2023-06-02',
      osStart: '2023-06-03',
      osEnd: '2023-06-03',
    });
    const r = enforceSettingsDates(s, '2023-06-01', '2023-06-03');
    expect(r.isStart).toBe('2023-06-01');
    expect(r.isEnd).toBe('2023-06-02');
    expect(r.osStart).toBe('2023-06-03');
    expect(r.osEnd).toBe('2023-06-03');
  });

  it('10. 1-day data range (impossible case): isEnd < osStart still holds', () => {
    // All four dates collapse to same day after clamping
    const s = makeSettings({
      isStart: '2023-06-01',
      isEnd: '2023-06-01',
      osStart: '2023-06-01',
      osEnd: '2023-06-01',
    });
    const r = enforceSettingsDates(s, '2023-06-01', '2023-06-01');
    // nextDay('2023-06-01') = '2023-06-02' but clamped to maxDate '2023-06-01'
    // so isEnd >= osStart safety check fires, pushing isEnd back
    // The result may be degenerate but the invariant isEnd < osStart should still hold
    // or the safety fallback sets isEnd = osStart - 1 day
    expect(r.isEnd < r.osStart).toBe(true);
  });

  it('11. No minDate/maxDate provided: should only enforce ordering, no clamping', () => {
    const s = makeSettings({
      isStart: '2023-08-01',
      isEnd: '2023-06-30',
      osStart: '2023-05-01',
      osEnd: '2023-04-01',
    });
    const r = enforceSettingsDates(s);
    expect(r.isStart <= r.isEnd).toBe(true);
    expect(r.isEnd < r.osStart).toBe(true);
    expect(r.osStart <= r.osEnd).toBe(true);
  });

  it('12. isEnd at maxDate boundary: nextDay(isEnd) exceeds maxDate, safety check fires', () => {
    const s = makeSettings({
      isStart: '2023-06-01',
      isEnd: '2023-06-30',
      osStart: '2023-06-30',
      osEnd: '2023-06-30',
    });
    const r = enforceSettingsDates(s, '2023-06-01', '2023-06-30');
    // isEnd >= osStart triggers nextDay, which becomes '2023-07-01' clamped to '2023-06-30'
    // safety check then pushes isEnd back to osStart - 1 day
    expect(r.isEnd < r.osStart).toBe(true);
    expect(r.osStart).toBe('2023-06-30');
    expect(r.isEnd).toBe('2023-06-29');
  });

  it('13. Timezone safety: dates near month/year boundaries', () => {
    const s = makeSettings({
      isStart: '2023-12-30',
      isEnd: '2023-12-31',
      osStart: '2023-12-31',
      osEnd: '2024-01-15',
    });
    const r = enforceSettingsDates(s, '2023-01-01', '2024-12-31');
    // isEnd >= osStart, so osStart = nextDay('2023-12-31') = '2024-01-01'
    expect(r.osStart).toBe('2024-01-01');
    expect(r.isEnd).toBe('2023-12-31');
    expect(r.osEnd).toBe('2024-01-15');
  });
});
