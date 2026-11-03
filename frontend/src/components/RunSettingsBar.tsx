'use client';

import { useRunStore } from '@/runStore';
import { useState } from 'react';
import type { RunSettings } from '@/types';

const inputClass = 'h-[19px] min-w-0 px-1 py-0 bg-transparent border border-transparent hover:border-[#777486] text-[11px] font-mono text-[#f0eff5] [color-scheme:dark] focus:outline-none focus:border-accent focus-visible:ring-1 focus-visible:ring-accent';
const rowClass = 'flex h-[19px] items-center gap-1 whitespace-nowrap';
const labelClass = 'text-[11px] text-[#c7c5d1]';

// Digits only while typing; commit on blur/Enter (empty -> previous value, out of range -> clamped).
function IntInput({ value, min, max, onCommit, ...rest }: {
  value: number; min: number; max: number; onCommit: (v: number) => void;
  disabled?: boolean; 'aria-label': string; autoComplete?: string; className?: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft === null) return;
    setDraft(null);
    if (draft === '') return;
    const next = Math.min(max, Math.max(min, parseInt(draft, 10)));
    if (next !== value) onCommit(next);
  };
  return (
    <input type="text" inputMode="numeric" value={draft ?? String(value)} {...rest}
      onChange={e => { if (/^\d*$/.test(e.target.value)) setDraft(e.target.value); }}
      onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') commit(); }} />
  );
}

export default function RunRunSettingsBar() {
  const settings = useRunStore(s => s.settings);
  const setSettings = useRunStore(s => s.setSettings);
  const config = useRunStore(s => s.config);
  const searchRound = useRunStore(s => s.searchRound);
  const isRunning = useRunStore(s => s.isRunning);

  const handleChange = (key: keyof RunSettings, value: string) => {
    if ((!value && key !== 'simEnd') || settings[key] === value) return;
    setSettings({ [key]: value });
  };

  return (
    <div className="research-toolbar flex w-fit max-w-full shrink-0 overflow-x-auto items-start gap-x-7 px-3 py-1 bg-[#353446] text-[#f0eff5]">
      <div className="grid grid-rows-3">
        <div className={rowClass}>
          <span className={labelClass}>Sim period:</span>
          <span className={labelClass}>From</span>
          <input type="date" value={settings.simStart} min={config?.simStart} max={settings.simEnd || config?.simEnd}
            onChange={e => handleChange('simStart', e.target.value)} aria-label="Sim start date"
            className={`${inputClass} w-[118px]`} />
          <span className={labelClass}>To</span>
          <input type="date" value={settings.simEnd}
            min={settings.simStart} max={config?.simEnd} disabled={isRunning}
            onChange={e => handleChange('simEnd', e.target.value)} aria-label="Sim end date"
            className={`${inputClass} w-[118px] disabled:opacity-60`} />
        </div>
        <label className={rowClass}>
          <span className={`${labelClass} w-[54px]`}>IS start:</span>
          <input type="date" value={settings.isStart} min={settings.simStart} max={settings.simEnd || config?.simEnd}
            onChange={e => handleChange('isStart', e.target.value)} aria-label="IS start date"
            className={`${inputClass} w-[118px]`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[54px]`}>OS period:</span>
          <input type="date" value={settings.osStart} min={settings.isStart} max={settings.simStart === settings.simEnd ? settings.osStart : (settings.simEnd || config?.simEnd)}
            onChange={e => handleChange('osStart', e.target.value)} aria-label="OS start date"
            className={`${inputClass} w-[118px]`} />
        </label>
      </div>

      <div className="grid grid-rows-3">
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>#Threads:</span>
          <IntInput min={1} max={32} value={settings.threads} disabled={isRunning}
            onCommit={v => setSettings({ threads: v })}
            aria-label="Number of threads" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>First search:</span>
          <IntInput min={1} max={5000} value={settings.firstSearch ?? 200} disabled={isRunning}
            onCommit={v => setSettings({ firstSearch: v })}
            aria-label="First search" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
      </div>

      <div className="grid grid-rows-3">
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>Stop round:</span>
          <IntInput min={1} max={1000} value={settings.stopRound ?? 40} disabled={isRunning}
            onCommit={v => setSettings({ stopRound: v })}
            aria-label="Stop round" className={`${inputClass} w-14 disabled:opacity-60`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>Stop alphas:</span>
          <IntInput min={1} max={5000} value={settings.stopAlphas ?? 400} disabled={isRunning}
            onCommit={v => setSettings({ stopAlphas: v })}
            aria-label="Stop alphas" className={`${inputClass} w-14 disabled:opacity-60`} />
        </label>
      </div>

      <div className="grid grid-rows-3 text-[11px]">
        <div className={rowClass} role="status" aria-live="polite">
          <span className={labelClass}>Status:</span>
          <span className={isRunning ? 'text-[#ffbe50]' : ''}>
            {searchRound !== null ? `GA · Round ${searchRound}` : isRunning ? 'Running' : 'Stopped'}
          </span>
        </div>
        <div className={rowClass}>
          <span className={labelClass}>Data end:</span>
          <span className="font-mono">{config?.simEnd ?? '—'}</span>
          <label className="ml-2 flex items-center gap-1">
            <span className={labelClass}>Execution:</span>
            <select aria-label="Execution price"
              value={config?.executionPrices?.length ? settings.executionPrice : (config?.executionPrice ?? 'close')}
              disabled={isRunning || !config?.executionPrices?.length}
              onChange={e => handleChange('executionPrice', e.target.value)}
              className={`${inputClass} w-[70px] disabled:opacity-60`}>
              {(config?.executionPrices ?? [config?.executionPrice ?? 'close']).map(price => (
                <option className="bg-[#353446]" key={price} value={price}>
                  {price === 'ohlc4' ? 'OHLC4' : price === 'vwap' ? 'VWAP' : 'Close'}
                </option>
              ))}
            </select>
          </label>
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
