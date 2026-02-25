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

    // Only update if value actually changed
    const currentValue = settings[key];
    if (currentValue === value) return;

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
      const newOsStart = nextDay(value);
      if (settings.isEnd !== value || settings.osStart !== newOsStart) {
        setSettings({ isEnd: value, osStart: newOsStart });
      }
    } else if (key === 'isStart') {
      const patch: Partial<BacktestSettings> = { isStart: value };
      if (value > settings.isEnd) {
        patch.isEnd = value;
        patch.osStart = nextDay(value);
      }
      // Only update if something actually changed
      const hasChanges = settings.isStart !== value || 
        ('isEnd' in patch && patch.isEnd !== settings.isEnd) ||
        ('osStart' in patch && patch.osStart !== settings.osStart);
      if (hasChanges) {
        setSettings(patch);
      }
    } else if (key === 'osEnd') {
      const patch: Partial<BacktestSettings> = { osEnd: value };
      if (value < settings.osStart) {
        patch.osStart = value;
        patch.isEnd = prevDay(value);
      }
      // Only update if something actually changed
      const hasChanges = settings.osEnd !== value ||
        ('osStart' in patch && patch.osStart !== settings.osStart) ||
        ('isEnd' in patch && patch.isEnd !== settings.isEnd);
      if (hasChanges) {
        setSettings(patch);
      }
    } else {
      setSettings({ [key]: value });
    }
  };

  return (
    <div className="flex items-center gap-4 px-4 py-2 bg-background-secondary border-b border-border">
      {/* IS Period */}
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide">IS</span>
        <input
          type="date"
          value={settings.isStart}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('isStart', e.target.value)}
          aria-label="IS start date"
          className="px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
        <span className="text-foreground-muted">~</span>
        <input
          type="date"
          value={settings.isEnd}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('isEnd', e.target.value)}
          aria-label="IS end date"
          className="px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>

      {/* OS Period */}
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide">OS</span>
        <input
          type="date"
          value={settings.osStart}
          readOnly
          tabIndex={-1}
          aria-label="OS start date"
          className="px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground-muted cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent"
        />
        <span className="text-foreground-muted">~</span>
        <input
          type="date"
          value={settings.osEnd}
          min={dataRange?.minDate}
          max={dataRange?.maxDate}
          onChange={(e) => handleChange('osEnd', e.target.value)}
          aria-label="OS end date"
          className="px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>

      {/* Bar Interval */}
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide">Interval</span>
        <select
          value={settings.barInterval}
          onChange={(e) => handleChange('barInterval', e.target.value as BacktestSettings['barInterval'])}
          aria-label="Bar interval"
          className="w-16 px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
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
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide">Jobs</span>
        <input
          type="number"
          min={1}
          max={32}
          value={settings.nJobs}
          onChange={(e) => setSettings({ nJobs: Math.max(1, parseInt(e.target.value) || 1) })}
          aria-label="Number of jobs"
          autoComplete="off"
          className="w-14 px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>

      {/* Population */}
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide">Population</span>
        <input
          type="number"
          min={10}
          max={500}
          value={settings.gaPopulation}
          onChange={(e) => setSettings({ gaPopulation: Math.max(10, parseInt(e.target.value) || 50) })}
          aria-label="GA population size"
          autoComplete="off"
          className="w-16 px-2 py-1 bg-background-elevated border border-border text-xs font-mono text-foreground focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>

      {/* GA Progress / Symbol Count */}
      <div className="flex items-center gap-1 ml-auto">
        {gaProgress ? (
          <span className="text-xs text-accent font-mono tabular-nums">
            {gaProgress.generation} : {gaProgress.evaluated}/{gaProgress.populationSize}
          </span>
        ) : (
          symbols.length > 0 && <span className="text-xs text-foreground-muted">{symbols.length} symbols</span>
        )}
      </div>
    </div>
  );
}
