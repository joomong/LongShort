'use client';

import { useEffect, useMemo, useRef } from 'react';
import { getSavedBatch, runCombinedAlphas } from '@/lib/api';
import { gaCombinedGroup, pendingGACombined } from '@/lib/gaCombined';
import { useAlphaStore } from '@/store/alphaStore';

export default function GACombinedLoader() {
  const session = useAlphaStore(s => s.gaSession);
  const key = useAlphaStore(s => s.gaCombinedKey);
  const cached = useAlphaStore(s => s.gaCombinedCache[s.gaCombinedKey]);
  const alphas = useAlphaStore(s => s.result?.alphas);
  const tab = useAlphaStore(s => s.activeResultTab);
  const ready = useAlphaStore(s => s.activeResultTab === 'stats' ? !s.isRunning
    : s.gaRounds.find(round => round.round === s.activeResultTab)?.status !== 'running');
  const settings = useAlphaStore(s => s.result?.runSettings);
  const revision = useAlphaStore(s => s.resultRevision);
  const savedSource = useAlphaStore(s => s.result?.savedSource);
  const group = useMemo(() => gaCombinedGroup(alphas ?? [], tab), [alphas, tab]);
  const work = useRef<{ key: string; controller: AbortController } | null>(null);

  // Keep an in-flight request when Round and Stats have identical membership.
  useEffect(() => () => { work.current?.controller.abort(); work.current = null; }, [key, revision]);
  useEffect(() => {
    if (!session || !key || !ready || cached || !settings || work.current?.key === key) return;
    const controller = new AbortController();
    work.current = { key, controller };
    // The saved batch covers exactly the Stats membership; other round batches still use the engine.
    const saved = savedSource && key === gaCombinedGroup(alphas ?? [], 'stats').key;
    (saved ? getSavedBatch(savedSource, controller.signal) : runCombinedAlphas(group.xmls, settings, controller.signal)).then(combined => {
      if (!controller.signal.aborted) useAlphaStore.getState().cacheGACombined(key, combined, revision);
    }).catch(error => {
      if (!controller.signal.aborted) useAlphaStore.getState().cacheGACombined(key,
        { ...pendingGACombined(), detailError: String(error) }, revision);
    }).finally(() => { if (work.current?.controller === controller) work.current = null; });
  }, [session, key, ready, cached, settings, revision, group, savedSource, alphas]);
  return null;
}
