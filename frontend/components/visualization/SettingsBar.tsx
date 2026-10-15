'use client';

import { useAlphaStore } from '@/store/alphaStore';
import type { BacktestSettings } from '@/types';

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
          <input type="date" value={config?.supportsSimEnd ? settings.simEnd : (config?.simEnd ?? '')}
            min={settings.simStart} max={config?.simEnd} disabled={isRunning || !config?.supportsSimEnd}
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
          <input type="number" min={1} max={32} value={settings.nJobs} disabled={isRunning}
            onChange={e => setSettings({ nJobs: Math.min(32, Math.max(1, parseInt(e.target.value) || 1)) })}
            aria-label="Number of threads" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>First search:</span>
          <input type="number" min={1} max={5000} value={settings.gaFirstSearch ?? 200} disabled={isRunning}
            onChange={e => setSettings({ gaFirstSearch: Math.min(5000, Math.max(1, parseInt(e.target.value) || 1)) })}
            aria-label="First search" autoComplete="off" className={`${inputClass} w-14`} />
        </label>
      </div>

      <div className="grid grid-rows-3">
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>Stop round:</span>
          <input type="number" min={1} max={1000} value={settings.gaStopRound ?? 40} disabled={isRunning}
            onChange={e => setSettings({ gaStopRound: Math.min(1000, Math.max(1, parseInt(e.target.value) || 1)) })}
            aria-label="Stop round" className={`${inputClass} w-14 disabled:opacity-60`} />
        </label>
        <label className={rowClass}>
          <span className={`${labelClass} w-[73px]`}>Stop alphas:</span>
          <input type="number" min={1} max={5000} value={settings.gaStopAlphas ?? 400} disabled={isRunning}
            onChange={e => setSettings({ gaStopAlphas: Math.min(5000, Math.max(1, parseInt(e.target.value) || 1)) })}
            aria-label="Stop alphas" className={`${inputClass} w-14 disabled:opacity-60`} />
        </label>
      </div>

      <div className="grid grid-rows-3 text-[11px]">
        <div className={rowClass} role="status" aria-live="polite">
          <span className={labelClass}>Status:</span>
          <span className={isRunning ? 'text-[#ffbe50]' : ''}>
            {gaProgress ? `GA · Round ${gaProgress.generation}` : isRunning ? 'Running' : 'Stopped'}
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
