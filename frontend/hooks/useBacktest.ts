'use client';

import { useCallback, useRef, useEffect } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { runSimulate, runGAStream, adaptSimulateResponse, adaptGACompleteToResponse } from '@/lib/api';
import type { StarGAEvent, BacktestSettings } from '@/types';

function executionMode(interval: BacktestSettings['barInterval']): 0 | 1 {
  if (interval === 'D0') return 0;
  if (interval === 'D1') return 1;
  throw new Error('Intraday execution is not available.');
}

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
    store.setGAProgress(null);

    try {
      const requestXml = xmlCodeRef.current;
      const settings = settingsRef.current;
      const mode = executionMode(settings.barInterval);
      const resp = await runSimulate(
        requestXml,
        settings.simStart,
        settings.isStart,
        settings.osStart,
        abortControllerRef.current.signal,
        settings.nJobs,
        mode,
      );

      if (requestId !== requestIdRef.current) return;

      const adapted = adaptSimulateResponse(resp);
      adapted.runSettings = { simStart: settings.simStart, isStart: settings.isStart, osStart: settings.osStart, mode, threads: settings.nJobs };

      const s = useAlphaStore.getState();
      if (requestId !== requestIdRef.current) return;
      s.setResult(adapted);

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
      s.setIsRunning(false);
    }
  }, []);

  const runGA = useCallback(async () => {
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
    store.setGAProgress({
      generation: 0,
      totalGenerations: 0,
      evaluated: 0,
      populationSize: settings.gaPopulation,
    });

    try {
      const requestXml = xmlCodeRef.current;
      const mode = executionMode(settings.barInterval);
      await runGAStream(
        requestXml,
        settings.simStart,
        settings.isStart,
        settings.osStart,
        settings.gaPopulation,
        20,  // generations (default)
        settings.nJobs,
        (event: StarGAEvent) => {
          if (requestId !== requestIdRef.current) return;
          const s = useAlphaStore.getState();
          if (event.type === 'generation_end') {
            s.setGAProgress({
              generation: event.generation,
              totalGenerations: event.totalGenerations,
              evaluated: 0,
              populationSize: settings.gaPopulation,
              bestSharpe: event.bestSharpe,
            });
          } else if (event.type === 'complete') {
            const adapted = adaptGACompleteToResponse(event);
            adapted.runSettings = { simStart: settings.simStart, isStart: settings.isStart, osStart: settings.osStart, mode, threads: settings.nJobs };
            if (adapted.alphas.length > 0) {
              adapted.alphas[0].xml = event.bestXml;
            }
            s.setResult(adapted);
            // Update XML to best found
            s.setXmlCode(event.bestXml);
          } else if (event.type === 'error') {
            s.setBacktestError(event.error);
          }
        },
        abortControllerRef.current.signal,
        mode,
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
    s.setIsRunning(false);
    s.setGAProgress(null);
  }, []);

  return { run, runGA, stop };
}
