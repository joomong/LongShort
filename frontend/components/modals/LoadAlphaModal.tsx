'use client';

import { useState, useEffect } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { getAlphaList, getAlpha } from '@/lib/api';
import type { AlphaInfo } from '@/types';

export default function LoadAlphaModal() {
  const isLoadModalOpen = useAlphaStore(s => s.isLoadModalOpen);
  const setIsLoadModalOpen = useAlphaStore(s => s.setIsLoadModalOpen);
  const setXmlCode = useAlphaStore(s => s.setXmlCode);
  const [alphas, setAlphas] = useState<AlphaInfo[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadingAlpha, setLoadingAlpha] = useState<string | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (isLoadModalOpen) {
      fetchAlphas();
    }
  }, [isLoadModalOpen]);

  const fetchAlphas = async () => {
    setIsLoading(true);
    setError('');

    try {
      const { alphas } = await getAlphaList();
      setAlphas(alphas);
    } catch {
      setError('Failed to fetch alpha list');
    } finally {
      setIsLoading(false);
    }
  };

  const handleLoad = async (name: string) => {
    setLoadingAlpha(name);
    setError('');

    try {
      const { xml } = await getAlpha(name);
      setXmlCode(xml);
      setIsLoadModalOpen(false);
    } catch {
      setError(`Failed to load ${name}`);
    } finally {
      setLoadingAlpha(null);
    }
  };

  const handleClose = () => {
    setError('');
    setIsLoadModalOpen(false);
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
                  <div>
                    <h3 className="font-medium text-foreground">{alpha.name}</h3>
                    <p className="text-xs text-foreground-secondary">
                      {alpha.createdAt ? new Date(alpha.createdAt).toLocaleString() : ''}
                    </p>
                  </div>
                  <button
                    onClick={() => handleLoad(alpha.name)}
                    disabled={loadingAlpha !== null}
                    className="px-3 py-1 text-sm bg-accent hover:bg-accent-hover text-white transition-colors disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {loadingAlpha === alpha.name ? 'Loading\u2026' : 'Load'}
                  </button>
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
