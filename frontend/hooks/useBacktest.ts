'use client';

import { useCallback, useRef } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { runGAStream } from '@/lib/api';
import type { BacktestRunResponse } from '@/types';

export function useBacktest() {
  const abortControllerRef = useRef<AbortController | null>(null);

  // Only subscribe to values actually read during render
  const xmlCode = useAlphaStore(s => s.xmlCode);
  const settings = useAlphaStore(s => s.settings);

  const run = useCallback(async () => {
    // Cancel any existing request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    abortControllerRef.current = new AbortController();

    // Read setters from getState() — no subscription needed
    const store = useAlphaStore.getState();
    store.setIsRunning(true);
    store.setBacktestError(null);
    store.setResult(null);
    store.setSelectedCell(null, null);
    store.setGAProgress({ generation: 0, totalGenerations: 0, evaluated: 0, populationSize: settings.gaPopulation });

    try {
      await runGAStream(
        xmlCode,
        settings,
        10,
        (event) => {
          const s = useAlphaStore.getState();
          const type = event.type as string;
          if (type === 'progress') {
            s.setGAProgress({
              generation: event.generation as number,
              totalGenerations: event.totalGenerations as number,
              evaluated: event.evaluated as number,
              populationSize: event.populationSize as number,
            });
          } else if (type === 'generation_end') {
            s.setGAProgress({
              generation: event.generation as number,
              totalGenerations: event.totalGenerations as number,
              evaluated: 0,
              populationSize: (event as Record<string, unknown>).populationSize as number || settings.gaPopulation,
            });
            const partial = event.partialResult as BacktestRunResponse | undefined;
            if (partial && partial.alphas && partial.alphas.length > 0) {
              s.setResult(partial);
            }
          } else if (type === 'complete') {
            const final = event.data as BacktestRunResponse;
            s.setResult(final);

            // UX: auto-select a default cell so the time series chart is not empty
            const firstSymbol = final?.heatmap?.symbols?.[0] ?? null;
            const firstTimestamp = final?.heatmap?.timestamps?.[0] ?? null;
            if (firstSymbol && firstTimestamp) {
              s.setSelectedCell(firstSymbol, firstTimestamp);
            }
          } else if (type === 'error') {
            s.setBacktestError(event.message as string);
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
  }, [xmlCode, settings]);

  const stop = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    const s = useAlphaStore.getState();
    s.setIsRunning(false);
    s.setGAProgress(null);
  }, []);

  return { run, stop };
}
