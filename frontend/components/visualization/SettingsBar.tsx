'use client';

import { useAlphaStore } from '@/store/alphaStore';
import type { BacktestSettings } from '@/types';

const BAR_INTERVALS: { value: BacktestSettings['barInterval']; label: string; disabled?: boolean }[] = [
  { value: 'D0', label: 'D0' },
  { value: 'D1', label: 'D1' },
  { value: 'intraday', label: 'intraday (coming soon)', disabled: true },
];

const inputClass = 'h-[19px] min-w-0 px-1 py-0 bg-transparent border border-transparent hover:border-[#777486] text-[11px] font-mono text-[#f0eff5] [color-scheme:dark] focus:outline-none focus:border-accent focus-visible:ring-1 focus-visible:ring-accent';
const rowClass = 'flex h-[19px] items-center gap-1 whitespace-nowrap';
const labelClass = 'text-[11px] text-[#c7c5d1]';

export default function SettingsBar() {
  const settings = useAlphaStore(s => s.settings);
  const setSettings = useAlphaStore(s => s.setSettings);
  const config = useAlphaStore(s => s.config);
  const gaProgress = useAlphaStore(s => s.gaProgress);
  const isRunning = useAlphaStore(s => s.isRunning);

  const handleChange = (key: keyof BacktestSettings, value: string) => {
    if (!value || settings[key] === value) return;
    setSettings({ [key]: value });
  };

  return (
    <div className="research-toolbar flex w-fit max-w-full shrink-0 overflow-x-auto items-start gap-x-7 px-3 py-1 bg-[#353446] text-[#f0eff5]">
      <div className="grid grid-rows-3">
        <label className={rowClass}>
          <span className={`${labelClass} w-[54px]`}>Sim period:</span>
          <input type="date" value={settings.simStart} min={config?.simStart} max={settings.isStart}
            onChange={e => handleChange('simStart', e.target.value)} aria-label="Sim start date"
            className={`${inputClass} w-[118px]`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[54px]`}>IS start:</span>
          <input type="date" value={settings.isStart} min={config?.simStart} max={config?.simEnd}
            onChange={e => handleChange('isStart', e.target.value)} aria-label="IS start date"
            className={`${inputClass} w-[118px]`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[54px]`}>OS period:</span>
          <input type="date" value={settings.osStart} min={config?.simStart} max={config?.simEnd}
            onChange={e => handleChange('osStart', e.target.value)} aria-label="OS start date"
            className={`${inputClass} w-[118px]`} />
        </label>
      </div>

      <div className="grid grid-rows-3">
        <label className={rowClass}>
          <span className={`${labelClass} w-[61px]`}>Jobs:</span>
          <input type="number" min={1} max={32} value={settings.nJobs}
            onChange={e => setSettings({ nJobs: Math.min(32, Math.max(1, parseInt(e.target.value) || 1)) })}
            aria-label="Number of jobs" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[61px]`}>Population:</span>
          <input type="number" min={10} max={500} value={settings.gaPopulation}
            onChange={e => setSettings({ gaPopulation: Math.min(500, Math.max(10, parseInt(e.target.value) || 50)) })}
            aria-label="GA population size" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[61px]`}>Interval:</span>
          <select value={settings.barInterval} onChange={e => handleChange('barInterval', e.target.value)}
            aria-label="Bar interval" className={`${inputClass} w-14`}>
            {BAR_INTERVALS.map(interval => (
              <option className="bg-[#353446]" key={interval.value} value={interval.value} disabled={interval.disabled}>
                {interval.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="grid grid-rows-3 text-[11px]">
        <div className={rowClass} role="status" aria-live="polite">
          <span className={labelClass}>Status:</span>
          <span className={isRunning ? 'text-[#ffbe50]' : ''}>
            {gaProgress ? `GA ${gaProgress.generation}: ${gaProgress.evaluated}/${gaProgress.populationSize}` : isRunning ? 'Running' : 'Stopped'}
          </span>
        </div>
        <div className={rowClass}>
          <span className={labelClass}>Data end:</span>
          <span className="font-mono">{config?.simEnd ?? '—'}</span>
        </div>
        {config?.universes && config.universes.length > 0 ? (
          <label className={rowClass}>
            <span className={labelClass}>Universe:</span>
            <select value={settings.universe} onChange={e => handleChange('universe', e.target.value)}
              aria-label="Universe" className={`${inputClass} w-[76px]`}>
              {config.universes.map(name => (
                <option className="bg-[#353446]" key={name} value={name}>{name}</option>
              ))}
            </select>
          </label>
        ) : (
          <div className={rowClass}>
            <span className={labelClass}>Universe:</span>
            <span>{config && config.N > 0 ? `${config.N} symbols` : '—'}</span>
          </div>
        )}
      </div>
    </div>
  );
}
