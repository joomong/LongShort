'use client';

import { useCallback, useRef, useEffect } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { runSimulate, runGAStream, adaptSingleResponse, adaptBatchResponse, adaptGACompleteToResponse } from '@/lib/api';
import type { StarGAEvent, StarSimulateResponse } from '@/types';

export function useBacktest() {
  const abortControllerRef = useRef<AbortController | null>(null);

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
    };
  }, []);

  const run = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();

    const store = useAlphaStore.getState();
    store.setIsRunning(true);
    store.setBacktestError(null);
    store.setResult(null);
    store.setSelectedCell(null, null);
    store.setGAProgress(null);

    try {
      const settings = settingsRef.current;
      const resp = await runSimulate(xmlCodeRef.current, settings.isStart, settings.osStart);

      // Detect single vs batch response
      const adapted = 'batches' in resp
        ? adaptBatchResponse(resp)
        : adaptSingleResponse(resp as StarSimulateResponse);

      const s = useAlphaStore.getState();
      s.setResult(adapted);

      // Auto-select first heatmap cell if available
      const firstSymbol = adapted.heatmap?.symbols?.[0] ?? null;
      const firstTimestamp = adapted.heatmap?.timestamps?.[0] ?? null;
      if (firstSymbol && firstTimestamp) {
        s.setSelectedCell(firstSymbol, firstTimestamp);
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        // cancelled
      } else {
        const message = error instanceof Error ? error.message : String(error);
        useAlphaStore.getState().setBacktestError(message);
      }
    } finally {
      const s = useAlphaStore.getState();
      s.setIsRunning(false);
    }
  }, []);

  const runGA = useCallback(async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = new AbortController();

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
      await runGAStream(
        xmlCodeRef.current,
        settings.isStart,
        settings.osStart,
        settings.gaPopulation,
        20,  // generations (default)
        settings.nJobs,
        (event: StarGAEvent) => {
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
            s.setResult(adapted);
            // Update XML to best found
            s.setXmlCode(event.bestXml);
          } else if (event.type === 'error') {
            s.setBacktestError(event.error);
          }
        },
        abortControllerRef.current.signal,
      );
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        // cancelled
      } else {
        const message = error instanceof Error ? error.message : String(error);
        useAlphaStore.getState().setBacktestError(message);
      }
    } finally {
      const s = useAlphaStore.getState();
      s.setIsRunning(false);
      s.setGAProgress(null);
    }
  }, []);

  const stop = useCallback(() => {
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
