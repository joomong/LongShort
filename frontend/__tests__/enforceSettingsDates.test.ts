import { describe, it, expect } from 'vitest';
import { enforceSettingsDates } from '@/store/alphaStore';
import type { BacktestSettings } from '@/types';

/** Helper to build a BacktestSettings object with date overrides. */
function makeSettings(overrides: Partial<BacktestSettings> = {}): BacktestSettings {
  return {
    simStart: '2020-01-01', simEnd: '',
    isStart: '2023-01-01',
    osStart: '2024-01-01',

    executionPrice: 'close',
    universe: '',
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
    expect(r.osStart).toBe('2024-01-01');
  });

  it('2. isStart >= osStart should push osStart forward via nextDay', () => {
    const s = makeSettings({ isStart: '2023-07-01', osStart: '2023-07-01' });
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.osStart).toBe('2023-07-02');
    expect(r.isStart < r.osStart).toBe(true);
  });

  it('3. isStart after osStart should push osStart to nextDay(isStart)', () => {
    const s = makeSettings({ isStart: '2023-08-01', osStart: '2023-06-01' });
    const r = enforceSettingsDates(s, '2020-01-01', '2025-12-31');
    expect(r.osStart).toBe('2023-08-02');
    expect(r.isStart < r.osStart).toBe(true);
  });

  it('4. Clamping to minDate: isStart before minDate should be clamped', () => {
    const s = makeSettings({ isStart: '2019-01-01' });
    const r = enforceSettingsDates(s, '2022-01-01', '2025-12-31');
    expect(r.isStart).toBe('2022-01-01');
  });

  it('5. Clamping to maxDate: osStart after maxDate should be clamped', () => {
    const s = makeSettings({ osStart: '2030-01-01' });
    const r = enforceSettingsDates(s, '2020-01-01', '2024-06-30');
    expect(r.isStart).toBe(r.simStart);
    expect(r.osStart > r.isStart && r.osStart < r.simEnd).toBe(true);
  });

  it('6. Both dates outside range: clamp and enforce isStart < osStart', () => {
    const s = makeSettings({ isStart: '2019-01-01', osStart: '2019-07-01' });
    const r = enforceSettingsDates(s, '2023-01-01', '2025-12-31');
    expect(r.isStart >= '2023-01-01').toBe(true);
    expect(r.osStart >= '2023-01-01').toBe(true);
    expect(r.isStart < r.osStart).toBe(true);
  });

  it('7. No minDate/maxDate provided: should only enforce ordering, no clamping', () => {
    const s = makeSettings({ isStart: '2023-08-01', osStart: '2023-05-01' });
    const r = enforceSettingsDates(s);
    expect(r.isStart < r.osStart).toBe(true);
  });

  it('8. Timezone safety: dates near year boundary', () => {
    const s = makeSettings({ isStart: '2023-12-31', osStart: '2023-12-31' });
    const r = enforceSettingsDates(s, '2023-01-01', '2024-12-31');
    expect(r.osStart).toBe('2024-01-01');
    expect(r.isStart < r.osStart).toBe(true);
  });
});
