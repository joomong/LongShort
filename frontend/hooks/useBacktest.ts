'use client';

import { useCallback, useRef, useEffect } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { runGAStream, adaptGACompleteToResponse, adaptGACandidate } from '@/lib/api';
import type { StarGAEvent } from '@/types';

export function useBacktest() {
  const abortControllerRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  // Use refs to stabilize values that change frequently
  const xmlCodeRef = useRef(useAlphaStore.getState().xmlCode);
  const settingsRef = useRef(useAlphaStore.getState().settings);

  // Update refs when store values change with proper cleanup
  useEffect(() => {
    const unsubscribeXml = useAlphaStore.subscribe(state => {
      xmlCodeRef.current = state.xmlCode;
    });
    const unsubscribeSettings = useAlphaStore.subscribe(state => {
      settingsRef.current = state.settings;
    });

    return () => {
      unsubscribeXml();
      unsubscribeSettings();
      requestIdRef.current += 1;
      abortControllerRef.current?.abort();
    };
  }, []);

  const run = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();
    const requestId = ++requestIdRef.current;

    const store = useAlphaStore.getState();
    store.setIsRunning(true);
    store.setBacktestError(null);
    store.setResult(null);
    store.setSelectedCell(null, null);

    const settings = settingsRef.current;
    const executionPrice = store.config?.executionPrices?.length ? settings.executionPrice : store.config?.executionPrice;
    store.setGAProgress({
      generation: 0,
      totalGenerations: 0,
      evaluated: 0,
      populationSize: settings.gaFirstSearch ?? 200,
    });

    const requestXml = xmlCodeRef.current;
    store.beginGA({ simStart: settings.simStart, simEnd: settings.simEnd || store.config?.simEnd || '', isStart: settings.isStart, osStart: settings.osStart, threads: settings.nJobs, universe: settings.universe, executionPrice }, requestXml);

    try {
      await runGAStream(
        requestXml,
        settings.simStart,
        settings.isStart,
        settings.osStart,
        settings.gaFirstSearch ?? 200,
        settings.gaStopRound ?? 40,
        settings.nJobs,
        (event: StarGAEvent) => {
          if (requestId !== requestIdRef.current) return;
          const s = useAlphaStore.getState();
          if (event.type === 'search_start') {
            const current = s.result;
            if (current?.runSettings) useAlphaStore.setState({ result: { ...current, runSettings: { ...current.runSettings, simStart: event.simStart, simEnd: event.simEnd, universe: event.universe, executionPrice: event.executionPrice } } });
          } else if (event.type === 'round_start') {
            s.startGARound(event.round);
            s.setGAProgress({ generation: event.round, totalGenerations: settings.gaStopRound ?? 40, evaluated: 0, populationSize: event.size });
          } else if (event.type === 'candidate') {
            s.appendGACandidate(adaptGACandidate(event.candidate));
          } else if (event.type === 'round_end') {
            s.endGARound(event.round);
          } else if (event.type === 'search_complete') {
            s.finishGA();
          } else if (event.type === 'generation_end') {
            s.setGAProgress({
              generation: event.generation,
              totalGenerations: event.totalGenerations,
              evaluated: 0,
              populationSize: settings.gaPopulation,
              bestSharpe: event.bestSharpe,
            });
          } else if (event.type === 'complete') {
            const adapted = adaptGACompleteToResponse(event);
            adapted.runSettings = { simStart: event.simStart ?? settings.simStart, simEnd: event.simEnd ?? (store.config?.supportsSimEnd ? settings.simEnd : store.config?.simEnd) ?? '', isStart: settings.isStart, osStart: settings.osStart, threads: settings.nJobs, universe: settings.universe, executionPrice: event.executionPrice ?? executionPrice };
            // Compatible complete events already contain daily performance.
            adapted.sourceXml = requestXml;
            adapted.alphas = adapted.alphas.map(alpha => ({ ...alpha, detailLoaded: true }));
            if (adapted.alphas.length > 0 && !adapted.alphas[0].xml) adapted.alphas[0].xml = event.bestXml;
            s.setResult(adapted);
            s.finishGA();
          } else if (event.type === 'error') {
            s.stopGA();
            s.setBacktestError(event.error);
          }
        },
        abortControllerRef.current.signal,
        settings.universe,
        executionPrice,
        store.config?.supportsSimEnd ? settings.simEnd : undefined,
        { stopAlphas: settings.gaStopAlphas ?? 400, roundSize: settings.gaPopulation },
      );
    } catch (error) {
      if (requestId !== requestIdRef.current) return;
      if (error instanceof Error && error.name === 'AbortError') {
        // cancelled
      } else {
        const message = error instanceof Error ? error.message : String(error);
        useAlphaStore.getState().setBacktestError(message);
      }
    } finally {
      if (requestId !== requestIdRef.current) return;
      const s = useAlphaStore.getState();
      s.stopGA();
      s.setIsRunning(false);
      s.setGAProgress(null);
    }
  }, []);

  const stop = useCallback(() => {
    requestIdRef.current += 1;
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    const s = useAlphaStore.getState();
    s.stopGA();
    s.setIsRunning(false);
    s.setGAProgress(null);
  }, []);

  return { run, stop };
}
