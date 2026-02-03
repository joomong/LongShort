'use client';

import { useAlphaStore } from '@/store/alphaStore';
import type { BacktestSettings } from '@/types';

const BAR_INTERVALS: { value: BacktestSettings['barInterval']; label: string; disabled?: boolean }[] = [
  { value: 'D0', label: 'D0' },
  { value: 'D1', label: 'D1' },
  { value: 'intraday', label: 'intraday (coming soon)', disabled: true },
];

export default function SettingsBar() {
  const settings = useAlphaStore(s => s.settings);
  const setSettings = useAlphaStore(s => s.setSettings);
  const dataRange = useAlphaStore(s => s.dataRange);
  const symbols = useAlphaStore(s => s.symbols);
  const gaProgress = useAlphaStore(s => s.gaProgress);

  const handleChange = (key: keyof BacktestSettings, value: string) => {
    if (!value) return;

    const nextDay = (d: string) => {
      const dt = new Date(d + 'T12:00:00Z'); // noon UTC avoids DST/timezone issues
      dt.setUTCDate(dt.getUTCDate() + 1);
      return dt.toISOString().slice(0, 10);
    };
    const prevDay = (d: string) => {
      const dt = new Date(d + 'T12:00:00Z'); // noon UTC avoids DST/timezone issues
      dt.setUTCDate(dt.getUTCDate() - 1);
      return dt.toISOString().slice(0, 10);
    };

    if (key === 'isEnd') {
      setSettings({ isEnd: value, osStart: nextDay(value) });
    } else if (key === 'isStart') {
      const patch: Partial<BacktestSettings> = { isStart: value };
      if (value > settings.isEnd) {
        patch.isEnd = value;
        patch.osStart = nextDay(value);
      }
      setSettings(patch);
    } else if (key === 'osEnd') {
      const patch: Partial<BacktestSettings> = { osEnd: value };
      if (value < settings.osStart) {
        patch.osStart = value;
        patch.isEnd = prevDay(value);
      }
      setSettings(patch);
    } else {
      setSettings({ [key]: value });
    }
  };

  return (
    <div className="flex items-center gap-4 px-4 py-1.5 bg-background-tertiary border-b border-border">
      {/* IS Period */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-foreground-secondary font-medium">IS:</span>
        <input
          type="date"
          value={settings.isStart}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('isStart', e.target.value)}
          className="px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        />
        <span className="text-foreground-muted">~</span>
        <input
          type="date"
          value={settings.isEnd}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('isEnd', e.target.value)}
          className="px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        />
      </div>

      {/* OS Period */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-foreground-secondary font-medium">OS:</span>
        <input
          type="date"
          value={settings.osStart}
          readOnly
          tabIndex={-1}
          className="px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground-muted cursor-not-allowed"
        />
        <span className="text-foreground-muted">~</span>
        <input
          type="date"
          value={settings.osEnd}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('osEnd', e.target.value)}
          className="px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        />
      </div>

      {/* Bar Interval */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-foreground-secondary font-medium">Interval:</span>
        <select
          value={settings.barInterval}
          onChange={(e) => handleChange('barInterval', e.target.value as BacktestSettings['barInterval'])}
          className="w-16 px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        >
          {BAR_INTERVALS.map((interval) => (
            <option key={interval.value} value={interval.value} disabled={interval.disabled}>
              {interval.label}
            </option>
          ))}
        </select>
      </div>

      {/* Jobs */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-foreground-secondary font-medium">Jobs:</span>
        <input
          type="number"
          min={1}
          max={32}
          value={settings.nJobs}
          onChange={(e) => setSettings({ nJobs: Math.max(1, parseInt(e.target.value) || 1) })}
          className="w-14 px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        />
      </div>

      {/* Population */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-foreground-secondary font-medium">Population:</span>
        <input
          type="number"
          min={10}
          max={500}
          value={settings.gaPopulation}
          onChange={(e) => setSettings({ gaPopulation: Math.max(10, parseInt(e.target.value) || 50) })}
          className="w-16 px-1.5 py-0.5 bg-background-elevated border border-border rounded text-xs text-foreground focus:outline-none focus:border-accent"
        />
      </div>

      {/* GA Progress / Symbol Count */}
      <div className="flex items-center gap-1 ml-auto">
        {gaProgress ? (
          <span className="text-xs text-accent font-mono">
            {gaProgress.generation} : {gaProgress.evaluated}/{gaProgress.populationSize}
          </span>
        ) : (
          symbols.length > 0 && <span className="text-xs text-foreground-muted">{symbols.length} symbols</span>
        )}
      </div>
    </div>
  );
}
