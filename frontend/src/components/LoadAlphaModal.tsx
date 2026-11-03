"use client";

import { useEffect, useRef, useState } from 'react';
import { useRunStore } from '@/runStore';
import { getAlphaList, getSavedResults } from '@/api';
import type { SavedAlphaInfo } from '@/types';

const LEVEL_LABEL = { xml: 'XML', results: 'Results', positions: 'Positions' } as const;

export default function LoadAlphaModal() {
  const isLoadModalOpen = useRunStore(state => state.isLoadModalOpen);
  const modalRevision = useRunStore(state => state.loadModalRevision);
  const setIsLoadModalOpen = useRunStore(state => state.setIsLoadModalOpen);
  const [alphas, setAlphas] = useState<SavedAlphaInfo[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadingAlpha, setLoadingAlpha] = useState<string | null>(null);
  const [error, setError] = useState('');
  const loadController = useRef<AbortController | null>(null);
  const requestId = useRef(0);

  useEffect(() => {
    if (!isLoadModalOpen) return;
    const controller = new AbortController();
    setIsLoading(true); setLoadingAlpha(null); setError('');
    getAlphaList(controller.signal).then(list => {
      if (!controller.signal.aborted) setAlphas(list);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => { if (!controller.signal.aborted) setIsLoading(false); });
    return () => {
      controller.abort(); loadController.current?.abort();
    };
  }, [isLoadModalOpen, modalRevision]);

  const handleLoad = async (alpha: SavedAlphaInfo) => {
    const initial = useRunStore.getState();
    if (alpha.level !== 'xml' && initial.isRunning) {
      setError('Stop the running search before loading saved results.'); return;
    }
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    const id = ++requestId.current, revision = initial.resultRevision, modal = initial.loadModalRevision;
    setLoadingAlpha(alpha.name); setError('');
    try {
      const saved = alpha.level === 'xml' ? undefined : await getSavedResults(alpha.name, controller.signal);
      const state = useRunStore.getState();
      if (controller.signal.aborted || id !== requestId.current || !state.isLoadModalOpen ||
          state.loadModalRevision !== modal || state.resultRevision !== revision) return;
      if (saved && state.isRunning) throw new Error('Stop the running search before loading saved results.');
      state.loadSavedAlpha(alpha, saved);
      const universe = saved?.context.universe ?? alpha.metadata.universe;
      if (universe && state.config && universe !== state.config.defaultUniverse && !state.config.universes.includes(universe))
        setError(`Saved universe "${universe}" is unavailable. Original results are preserved; choose an available universe and start a new run to regenerate data.`);
      else setIsLoadModalOpen(false);
    } catch (reason) {
      if (!controller.signal.aborted && id === requestId.current)
        setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!controller.signal.aborted && id === requestId.current) setLoadingAlpha(null);
    }
  };

  const handleClose = () => {
    ++requestId.current; loadController.current?.abort();
    setError(''); setLoadingAlpha(null); setIsLoadModalOpen(false);
  };

  if (!isLoadModalOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-background-tertiary shadow-modal border border-border w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" style={{ overscrollBehavior: 'contain' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h2 className="text-lg font-semibold text-foreground">Load Alpha</h2>
          <button
            onClick={handleClose}
            className="text-foreground-secondary hover:text-foreground transition-colors focus-visible:ring-2 focus-visible:ring-accent"
            aria-label="Close"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-6 py-4">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <svg className="w-6 h-6 animate-spin text-accent" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              <span className="ml-2 text-foreground-secondary">Loading\u2026</span>
            </div>
          ) : alphas.length === 0 ? (
            <div className="text-center py-8 text-foreground-muted">
              No saved alphas found
            </div>
          ) : (
            <div className="space-y-2">
              {alphas.map((alpha) => (
                <div
                  key={alpha.name}
                  className="flex items-center justify-between p-3 bg-background-elevated border border-border hover:bg-border-hover transition-colors"
                >
                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2">
                      <h3 className="font-medium text-foreground">{alpha.name}</h3>
                      <span className="px-1 text-[10px] border border-border text-foreground-secondary" data-testid="level-badge">{LEVEL_LABEL[alpha.level] ?? alpha.level}</span>
                    </div>
                    <p className="text-xs text-foreground-secondary font-mono">
                      {alpha.metadata ? `${alpha.metadata.simStart} ~ ${alpha.metadata.simEnd} \u00b7 OS ${alpha.metadata.osStart} \u00b7 ${alpha.metadata.universe} \u00b7 ${alpha.metadata.executionPrice}` : ''}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1">
                  <button
                    onClick={() => handleLoad(alpha)}
                    disabled={loadingAlpha !== null}
                    className="px-3 py-1 text-sm bg-accent hover:bg-accent-hover text-white transition-colors disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {loadingAlpha === alpha.name ? 'Loading\u2026' : 'Load'}
                  </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Error */}
          {error && (
            <p className="mt-4 text-sm text-red-400">{error}</p>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end px-6 py-4 border-t border-border">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-foreground-secondary hover:text-foreground transition-colors focus-visible:ring-2 focus-visible:ring-accent"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
