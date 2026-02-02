'use client';

import { useCallback, useRef } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { runGAStream } from '@/lib/api';
import type { BacktestRunResponse } from '@/types';

export function useBacktest() {
  const abortControllerRef = useRef<AbortController | null>(null);

  const {
    xmlCode,
    settings,
    setResult,
    setIsRunning,
    setSelectedCell,
    setBacktestError,
    setGAProgress,
  } = useAlphaStore();

  const run = useCallback(async () => {
    // Cancel any existing request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    abortControllerRef.current = new AbortController();
    setIsRunning(true);
    setBacktestError(null);
    setResult(null);
    setSelectedCell(null, null);
    setGAProgress({ generation: 0, totalGenerations: 0, evaluated: 0, populationSize: settings.gaPopulation });

    try {
      await runGAStream(
        xmlCode,
        settings,
        10,
        (event) => {
          const type = event.type as string;
          if (type === 'progress') {
            setGAProgress({
              generation: event.generation as number,
              totalGenerations: event.totalGenerations as number,
              evaluated: event.evaluated as number,
              populationSize: event.populationSize as number,
            });
          } else if (type === 'generation_end') {
            setGAProgress({
              generation: event.generation as number,
              totalGenerations: event.totalGenerations as number,
              evaluated: 0,
              populationSize: (event as Record<string, unknown>).populationSize as number || settings.gaPopulation,
            });
            const partial = event.partialResult as BacktestRunResponse | undefined;
            if (partial && partial.alphas && partial.alphas.length > 0) {
              setResult(partial);
            }
          } else if (type === 'complete') {
            const final = event.data as BacktestRunResponse;
            setResult(final);

            // UX: auto-select a default cell so the time series chart is not empty
            const firstSymbol = final?.heatmap?.symbols?.[0] ?? null;
            const firstTimestamp = final?.heatmap?.timestamps?.[0] ?? null;
            if (firstSymbol && firstTimestamp) {
              setSelectedCell(firstSymbol, firstTimestamp);
            }
          } else if (type === 'error') {
            setBacktestError(event.message as string);
          }
        },
        abortControllerRef.current.signal,
      );
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        // cancelled
      } else {
        const message = error instanceof Error ? error.message : String(error);
        setBacktestError(message);
      }
    } finally {
      setIsRunning(false);
      setGAProgress(null);
    }
  }, [xmlCode, settings, setResult, setIsRunning, setSelectedCell, setBacktestError, setGAProgress]);

  const stop = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsRunning(false);
    setGAProgress(null);
  }, [setIsRunning, setGAProgress]);

  return { run, stop };
}
