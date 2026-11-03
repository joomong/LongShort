'use client';

import { useEffect, useRef } from 'react';
import { getConfig, getSavedBatch, getSavedRow, requestSimulation } from '@/api';
import { combinedMembers, selectSignalSource, useRunStore } from '@/runStore';
import type { RunContext, SelectedSignalSeries, SimulationOutput, SimulationRequest } from '@/types';

function requestFor(context: RunContext, output: SimulationOutput, xml?: string): SimulationRequest {
  const { simStart, simEnd, isStart, osStart, universe, executionPrice, threads } = context;
  return { simStart, simEnd, isStart, osStart, universe, executionPrice, threads, output, xml };
}
function unavailableUniverse(context: RunContext, available: string[], defaultUniverse: string) {
  return context.universe && context.universe !== defaultUniverse && !available.includes(context.universe)
    ? `The result's universe "${context.universe}" is unavailable. Choose a universe and start a new run to regenerate data.` : null;
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }

export function useResultData() {
  const configRevision = useRunStore(state => state.configRevision);
  const config = useRunStore(state => state.config);
  const result = useRunStore(state => state.result);
  const revision = useRunStore(state => state.resultRevision);
  const viewMode = useRunStore(state => state.viewMode);
  const selectedIndex = useRunStore(state => state.selectedAlphaIndex);
  const signalIndex = useRunStore(state => state.selectedSignalAlphaIndex);
  const selectedSymbol = useRunStore(state => state.selectedSymbol);
  const rounds = useRunStore(state => state.gaRounds);
  const tab = useRunStore(state => state.activeResultTab);
  const running = useRunStore(state => state.isRunning);
  const key = useRunStore(state => state.gaCombinedKey);
  const xmls = useRunStore(state => state.combinedXmls);
  const combined = useRunStore(state => state.gaCombinedCache[state.gaCombinedKey]);
  const retries = useRunStore(state => state.retries);
  const seriesCache = useRef(new Map<string, SelectedSignalSeries>());
  const context = result?.context;
  const savedSource = result?.savedSource;
  const selected = selectedIndex === null || selectedIndex < 0 ? undefined : result?.alphas[selectedIndex];
  const signalAlpha = signalIndex === null || signalIndex >= 0 ? result?.alphas[signalIndex ?? 0] : undefined;
  const selectedId = selected?.id;
  const selectedXml = selected?.xml;
  const selectedStatus = selected?.status;
  const selectedPerformance = selected?.performance;
  const signalId = signalAlpha?.id;
  const signalXml = signalAlpha?.xml;
  const signalStatus = signalAlpha?.status;
  const plot = selectSignalSource({ result, selectedSignalAlphaIndex: signalIndex })?.plot;
  const contextError = context && config ? unavailableUniverse(context, config.universes, config.defaultUniverse) : null;
  const detailReady = !rounds.some(round => round.status === 'running' && round.round === selected?.round);
  const combinedReady = tab === 'stats' ? !running : rounds.find(round => round.round === tab)?.status !== 'running';
  const single = result?.alphas.length === 1;
  const seriesKey = `${revision}:${signalIndex ?? (result?.combined ? -1 : 0)}:${selectedSymbol ?? ''}`;
  const needsFullSeries = viewMode === 'single' && single && !!plot?.sampled && plot.rowStride > 1 && !!selectedSymbol;
  const savedBatch = !!savedSource && key === combinedMembers(result?.alphas ?? [], 'stats').key;

  useEffect(() => {
    const state = useRunStore.getState();
    if (state.config && state.configRequest.status === 'ready') return;
    const controller = new AbortController();
    state.setConfigRequest({ status: 'loading' });
    getConfig(controller.signal).then(value => {
      if (!controller.signal.aborted) useRunStore.getState().setConfig(value);
    }).catch(error => {
      if (!controller.signal.aborted) useRunStore.getState().setConfigRequest({ status: 'error', error: message(error) });
    });
    return () => controller.abort();
  }, [configRevision]);

  useEffect(() => { seriesCache.current.clear(); }, [revision]);

  useEffect(() => {
    const state = useRunStore.getState();
    if (viewMode !== 'detail' || selectedIndex === null || selectedIndex < 0 || selectedId === undefined || selectedStatus === 'failed' || !detailReady || !context) return;
    if (selectedPerformance) { state.setRequestState('detail', { status: 'ready' }, revision); return; }
    if (!savedSource && contextError) { state.setRequestState('detail', { status: 'error', error: contextError }, revision); return; }
    if (!savedSource && !config) return;
    const controller = new AbortController();
    state.setRequestState('detail', { status: 'loading' }, revision);
    const task = savedSource ? getSavedRow(savedSource, selectedId, controller.signal)
      : requestSimulation(requestFor(context, 'performance', selectedXml), controller.signal);
    task.then(response => {
      if (controller.signal.aborted) return;
      const detail = response.alphas[0];
      if (!detail?.performance) throw new Error('No performance returned for this alpha.');
      useRunStore.getState().cachePerformance(selectedIndex, detail, revision);
    }).catch(error => {
      if (!controller.signal.aborted) useRunStore.getState().setRequestState('detail', { status: 'error', error: message(error) }, revision);
    });
    return () => controller.abort();
  }, [viewMode, selectedIndex, selectedId, selectedXml, selectedStatus, selectedPerformance, detailReady, context, savedSource, revision, config, contextError, retries.detail]);

  useEffect(() => {
    const state = useRunStore.getState();
    if (viewMode !== 'single' || !single || signalId === undefined || signalStatus === 'failed' || !signalXml || !context || !config) return;
    if (plot) { state.setRequestState('signals', { status: 'ready' }, revision); return; }
    if (contextError) { state.setRequestState('signals', { status: 'error', error: contextError }, revision); return; }
    const index = signalIndex ?? 0;
    const controller = new AbortController();
    state.setRequestState('signals', { status: 'loading' }, revision);
    requestSimulation(requestFor(context, 'signals', signalXml), controller.signal).then(response => {
      if (controller.signal.aborted) return;
      const payload = response.alphas[0]?.signals;
      if (!payload?.plot) throw new Error('No signal data returned for this alpha.');
      useRunStore.getState().cacheSignals(index, payload, revision);
    }).catch(error => {
      if (!controller.signal.aborted) useRunStore.getState().setRequestState('signals', { status: 'error', error: message(error) }, revision);
    });
    return () => controller.abort();
  }, [viewMode, single, signalIndex, signalId, signalXml, signalStatus, plot, context, config, contextError, revision, retries.signals]);

  useEffect(() => {
    const state = useRunStore.getState();
    if (!needsFullSeries || !signalXml || !selectedSymbol || !context || !config) return;
    if (state.selectedSeries?.key === seriesKey) return;
    if (contextError) { state.setRequestState('series', { status: 'error', error: contextError }, revision); return; }
    const cached = seriesCache.current.get(seriesKey);
    if (cached) {
      seriesCache.current.delete(seriesKey); seriesCache.current.set(seriesKey, cached);
      state.setSelectedSeries(cached, revision);
      return;
    }
    const controller = new AbortController();
    state.setRequestState('series', { status: 'loading' }, revision);
    requestSimulation({ ...requestFor(context, 'signalSeries', signalXml), signalPlotSymbols: [selectedSymbol] }, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      const series = response.alphas[0]?.signals?.plot;
      if (!series || series.symbols[0] !== selectedSymbol) throw new Error('No selected-symbol series returned.');
      const full: SelectedSignalSeries = { key: seriesKey, timestamps: series.timestamps,
        values: series.values.map(row => row[0] ?? null), membership: series.values.map((_, row) => series.membership?.[row]?.[0] !== '0') };
      seriesCache.current.set(seriesKey, full);
      if (seriesCache.current.size > 32) seriesCache.current.delete(seriesCache.current.keys().next().value!);
      useRunStore.getState().setSelectedSeries(full, revision);
    }).catch(error => {
      if (!controller.signal.aborted) useRunStore.getState().setRequestState('series', { status: 'error', error: message(error) }, revision);
    });
    return () => controller.abort();
  }, [needsFullSeries, seriesKey, signalXml, selectedSymbol, context, config, contextError, revision, retries.series]);

  useEffect(() => {
    const state = useRunStore.getState();
    if (!key || !combinedReady || !context || !state.gaSession) return;
    if (combined) { state.setRequestState('combined', { status: 'ready' }, revision); return; }
    if (!savedBatch && contextError) { state.setRequestState('combined', { status: 'error', error: contextError }, revision); return; }
    if (!savedBatch && !config) return;
    const controller = new AbortController();
    state.setRequestState('combined', { status: 'loading' }, revision);
    const task = savedBatch ? getSavedBatch(savedSource!, controller.signal)
      : requestSimulation({ ...requestFor(context, 'performance'), xmls, combinedOnly: true }, controller.signal);
    task.then(response => {
      if (controller.signal.aborted) return;
      if (!response.combined?.performance) throw new Error('No combined performance returned.');
      useRunStore.getState().cacheCombined(key, response.combined, revision);
    }).catch(error => {
      if (!controller.signal.aborted) useRunStore.getState().setRequestState('combined', { status: 'error', error: message(error) }, revision);
    });
    return () => controller.abort();
  }, [key, xmls, combinedReady, combined, context, savedSource, savedBatch, config, contextError, revision, retries.combined]);
}
