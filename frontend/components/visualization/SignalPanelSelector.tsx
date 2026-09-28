'use client';

import { useEffect, useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { adaptSignalPayload, runSimulate } from '@/lib/api';
import { selectSignalSource } from '@/lib/signalSelection';

export default function SignalPanelSelector() {
  const result = useAlphaStore(s => s.result);
  const hasResult = result !== null;
  const isSingleAlpha = result?.alphas.length === 1;
  const revision = useAlphaStore(s => s.resultRevision);
  const selected = useAlphaStore(s => s.selectedSignalAlphaIndex);
  const viewMode = useAlphaStore(s => s.viewMode);
  const currentSettings = useAlphaStore(s => s.settings);
  const source = useAlphaStore(selectSignalSource);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const index = selected ?? (result?.combined ? -1 : 0);
  const alpha = index >= 0 ? result?.alphas[index] : result?.combined;
  const xml = alpha?.xml;
  const heatmap = source?.heatmap;
  const { simStart, isStart, osStart } = result?.runSettings ?? currentSettings;
  const mode = result?.runSettings?.mode ?? (currentSettings.barInterval === 'D0' ? 0 : 1);
  const threads = result?.runSettings?.threads ?? currentSettings.nJobs;
  const universe = result?.runSettings?.universe ?? currentSettings.universe;

  useEffect(() => {
    setError('');
    setLoading(false);
    if (!hasResult || !isSingleAlpha || viewMode === 'detail' || heatmap || index < 0) return;
    if (!xml) {
      setError('This alpha has no saved XML to load its signals.');
      return;
    }
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    runSimulate(xml, simStart, isStart, osStart, controller.signal, threads, mode, { universe }).then(response => {
      if (!active) return;
      const batch = response.batches[0];
      const signals = adaptSignalPayload(batch?.signalPlot ? batch : response);
      if (!signals.heatmap) throw new Error('The engine returned no signal data for this alpha.');
      useAlphaStore.getState().cacheAlphaSignals(index, signals, revision);
    }).catch(reason => {
      if (active && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; controller.abort(); };
    // result identity can change when another alpha's signal cache is populated.
    // The revision identifies the original backtest and guards stale responses.
  }, [hasResult, isSingleAlpha, revision, index, xml, heatmap, simStart, isStart, osStart, mode, threads, universe, viewMode, retry]);

  if (!result || !isSingleAlpha || (!loading && !error)) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 py-1 border-b border-border bg-background-secondary text-[11px]">
      {loading ? <span role="status">Loading signals…</span> : null}
      {error ? <span role="alert" className="text-metric-negative">{error} <button onClick={() => setRetry(value => value + 1)} className="underline">Retry signals</button></span> : null}
    </div>
  );
}
