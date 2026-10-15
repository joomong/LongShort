'use client';

import { useEffect } from 'react';
import { adaptSimulateResponse, getSavedRow, runSimulate } from '@/lib/api';
import { useAlphaStore } from '@/store/alphaStore';

const KEEP = new Set(['name', 'rank', 'xml', 'heatmap', 'signalRange', 'distribution', 'signalDistributionLog',
  'distributionBins', 'distributionRange', 'distributionExamples']);

export default function GADetailLoader() {
  const session = useAlphaStore(s => s.gaSession);
  const index = useAlphaStore(s => s.selectedAlphaIndex);
  const alpha = useAlphaStore(s => s.selectedAlphaIndex === null ? undefined : s.result?.alphas[s.selectedAlphaIndex]);
  const settings = useAlphaStore(s => s.result?.runSettings);
  const revision = useAlphaStore(s => s.resultRevision);
  const savedSource = useAlphaStore(s => s.result?.savedSource);
  const evaluating = useAlphaStore(s => s.gaRounds.some(round => round.status === 'running' &&
    round.round === s.result?.alphas[s.selectedAlphaIndex ?? -1]?.round));
  useEffect(() => {
    if (!session || evaluating || index === null || !alpha?.xml || alpha.error || alpha.detailLoaded || alpha.detailError || !settings) return;
    const controller = new AbortController();
    // A loaded saved result reads the row file written at save time instead of re-running the engine.
    const request = savedSource && alpha.candidateId !== undefined
      ? getSavedRow(savedSource, alpha.candidateId, controller.signal)
      : runSimulate(alpha.xml, settings.simStart, settings.isStart, settings.osStart, controller.signal, settings.threads,
        { simEnd: settings.simEnd, universe: settings.universe, executionPrice: settings.executionPrice, metricsOnly: true });
    request
      .then(response => {
        if (controller.signal.aborted) return;
        const detail = adaptSimulateResponse(response).alphas[0];
        if (!detail) throw new Error('No detail returned for this alpha.');
        // The candidate's ID/name and round remain stable as other results arrive; a metrics-only body
        // carries null signal fields that must not erase signals already loaded for this alpha.
        const metrics = Object.fromEntries(Object.entries(detail).filter(([key, value]) => value !== undefined && !KEEP.has(key)));
        useAlphaStore.getState().cacheGADetail(index, { ...metrics, detailLoaded: true }, revision);
      }).catch(error => {
        if (!controller.signal.aborted) useAlphaStore.getState().cacheGADetail(index, { detailError: String(error) }, revision);
      });
    return () => controller.abort();
  }, [session, evaluating, index, alpha?.xml, alpha?.error, alpha?.detailLoaded, alpha?.detailError, alpha?.candidateId, settings, revision, savedSource]);
  return null;
}
