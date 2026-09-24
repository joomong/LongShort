'use client';

import { useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { saveAlpha } from '@/lib/api';

export default function SaveAlphaModal() {
  const isSaveModalOpen = useAlphaStore(s => s.isSaveModalOpen);
  const setIsSaveModalOpen = useAlphaStore(s => s.setIsSaveModalOpen);
  const result = useAlphaStore(s => s.result);
  const selectedAlphaIndices = useAlphaStore(s => s.selectedAlphaIndices);
  const clearAlphaSelection = useAlphaStore(s => s.clearAlphaSelection);
  const [name, setName] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const selectedAlphas = selectedAlphaIndices.map(index => result?.alphas[index]);

  if (!isSaveModalOpen) return null;

  const handleSave = async () => {
    if (!name.trim()) {
      setError('Please enter a name');
      return;
    }

    if (selectedAlphas.length === 0) {
      setError('Select alphas in the result table before saving.');
      return;
    }
    if (selectedAlphas.some(alpha => !alpha?.xml?.trim())) {
      setError('A selected alpha has no XML to save. Run it again before saving.');
      return;
    }

    setIsLoading(true);
    setError('');

    try {
      // Explicit table selection always determines the save targets, including in detail view.
      for (const alpha of selectedAlphas) {
        const saveName = selectedAlphas.length === 1 ? name.trim() : `${name.trim()}_${alpha!.name}`;
        await saveAlpha(saveName, alpha!.xml!);
      }
      clearAlphaSelection();
      setName('');
      setIsSaveModalOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setIsLoading(false);
    }
  };

  const handleClose = () => {
    setName('');
    setError('');
    setIsSaveModalOpen(false);
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-background-tertiary shadow-modal border border-border w-full max-w-md mx-4" style={{ overscrollBehavior: 'contain' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h2 className="text-lg font-semibold text-foreground">Save Alpha</h2>
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
        <div className="px-6 py-4 space-y-4">
          {/* Name Input */}
          <div>
            <label htmlFor="save-alpha-name" className="block text-sm text-foreground-secondary mb-2">Alpha Name</label>
            <input
              id="save-alpha-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., momentum_v1"
              autoComplete="off"
              className="w-full px-3 py-2 bg-background-elevated border border-border text-foreground placeholder-foreground-muted focus:outline-none focus:border-accent"
            />
          </div>

          {/* Selected alphas info */}
          {selectedAlphas.length > 0 && (
            <div>
              <label className="block text-sm text-foreground-secondary mb-1">Saving {selectedAlphas.length} alpha(s):</label>
              <ul className="text-xs text-foreground-muted font-mono max-h-24 overflow-y-auto space-y-0.5">
                {selectedAlphas.map((a, i) => (
                  <li key={i}>{a?.name ?? 'Unavailable alpha'}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Error */}
          {error && (
            <p role="alert" className="text-sm text-metric-negative">{error}</p>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-border">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-foreground-secondary hover:text-foreground transition-colors focus-visible:ring-2 focus-visible:ring-accent"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={isLoading || selectedAlphas.length === 0}
            className="px-4 py-2 bg-accent hover:bg-accent-hover text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent"
          >
            {isLoading ? 'Saving\u2026' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
