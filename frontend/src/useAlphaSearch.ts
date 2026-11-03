'use client';

import { useCallback, useEffect, useRef } from 'react';
import { streamAlphaSearch } from '@/api';
import { useRunStore } from '@/runStore';
import type { RunContext, SearchEvent } from '@/types';

export function useAlphaSearch() {
  const controller = useRef<AbortController | null>(null);
  const requestId = useRef(0);
  const flushPending = useRef<(() => void) | null>(null);

  const stopSearch = useCallback(() => {
    flushPending.current?.();
    flushPending.current = null;
    ++requestId.current;
    controller.current?.abort();
    controller.current = null;
    useRunStore.getState().finishRun('stopped');
  }, []);

  useEffect(() => () => {
    if (useRunStore.getState().isRunning) stopSearch();
    else { ++requestId.current; controller.current?.abort(); }
  }, [stopSearch]);

  const startSearch = useCallback(async () => {
    const store = useRunStore.getState();
    if (!store.config || store.configRequest.status !== 'ready') {
      store.setBacktestError('Load engine configuration before running.');
      return;
    }
    const settings = store.settings;
    if (settings.universe && settings.universe !== store.config.defaultUniverse && !store.config.universes.includes(settings.universe)) {
      store.setBacktestError(`Choose an available universe before running: ${settings.universe}`);
      return;
    }
    controller.current?.abort();
    const current = ++requestId.current;
    const abort = new AbortController();
    controller.current = abort;
    const sourceXml = store.xmlCode;
    const context: RunContext = { simStart: settings.simStart, simEnd: settings.simEnd || store.config.simEnd,
      isStart: settings.isStart, osStart: settings.osStart, universe: settings.universe,
      executionPrice: settings.executionPrice, threads: settings.threads,
      feeRate: store.config.feeRate, reportNotional: store.config.reportNotional };
    store.beginRun(context, sourceXml);
    let events: SearchEvent[] = [];
    let scheduled = false, terminal = false;
    const flush = () => {
      scheduled = false;
      if (current !== requestId.current || !events.length) return;
      const received = events;
      events = [];
      useRunStore.getState().receiveSearchEvents(received);
    };
    flushPending.current = flush;
    try {
      await streamAlphaSearch({ xml: sourceXml, simStart: context.simStart, simEnd: context.simEnd,
        isStart: context.isStart, osStart: context.osStart, universe: context.universe,
        executionPrice: context.executionPrice, threads: context.threads,
        firstSearch: settings.firstSearch, roundSize: settings.roundSize,
        stopRound: settings.stopRound, stopAlphas: settings.stopAlphas }, event => {
        if (current !== requestId.current) return;
        events.push(event);
        if (event.type === 'search_complete' || event.type === 'error') {
          terminal = true;
          flush();
        } else if (!scheduled) {
          scheduled = true;
          queueMicrotask(flush);
        }
      }, abort.signal);
    } catch (error) {
      if (current !== requestId.current || terminal) return;
      flush();
      if (!(error instanceof Error && error.name === 'AbortError'))
        useRunStore.getState().finishRun('stopped', error instanceof Error ? error.message : String(error));
    } finally {
      if (current === requestId.current) {
        flush();
        flushPending.current = null;
        controller.current = null;
        useRunStore.getState().finishRun('stopped');
      }
    }
  }, []);

  return { startSearch, stopSearch };
}
