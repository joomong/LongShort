'use client';

import { useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { saveAlpha } from '@/lib/api';

export default function SaveAlphaModal() {
  const { isSaveModalOpen, setIsSaveModalOpen, xmlCode, viewMode, result, selectedAlphaIndices, clearAlphaSelection } = useAlphaStore();
  const [name, setName] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const isMulti = viewMode === 'multi' && selectedAlphaIndices.length > 0;
  const selectedAlphas = isMulti
    ? selectedAlphaIndices.map((i) => result?.alphas?.[i]).filter(Boolean)
    : [];

  if (!isSaveModalOpen) return null;

  const handleSave = async () => {
    if (!name.trim()) {
      setError('Please enter a name');
      return;
    }

    setIsLoading(true);
    setError('');

    try {
      if (isMulti && selectedAlphas.length > 0) {
        // Save each selected alpha individually with name prefix
        for (let i = 0; i < selectedAlphas.length; i++) {
          const alpha = selectedAlphas[i]!;
          const saveName = selectedAlphas.length === 1
            ? name.trim()
            : `${name.trim()}_${alpha.name}`;
          await saveAlpha(saveName, alpha.name);
        }
        clearAlphaSelection();
      } else {
        await saveAlpha(name.trim(), xmlCode);
      }
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
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-background-tertiary rounded-lg shadow-xl w-full max-w-md mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border">
          <h2 className="text-lg font-semibold text-foreground">Save Alpha</h2>
          <button
            onClick={handleClose}
            className="text-foreground-secondary hover:text-foreground transition-colors"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-4 space-y-4">
          {/* Name Input */}
          <div>
            <label className="block text-sm text-foreground-secondary mb-2">Alpha Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., momentum_v1"
              className="w-full px-3 py-2 bg-background-elevated border border-border rounded text-foreground placeholder-foreground-muted focus:outline-none focus:border-accent"
            />
          </div>

          {/* Selected alphas info */}
          {isMulti && selectedAlphas.length > 0 && (
            <div>
              <label className="block text-sm text-foreground-secondary mb-1">Saving {selectedAlphas.length} alpha(s):</label>
              <ul className="text-xs text-foreground-muted font-mono max-h-24 overflow-y-auto space-y-0.5">
                {selectedAlphas.map((a, i) => (
                  <li key={i}>{a!.name}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Backend note */}
          <p className="text-xs text-foreground-muted">
            Note: backend currently saves XML only (no signal/Parquet save).
          </p>

          {/* Error */}
          {error && (
            <p className="text-sm text-red-400">{error}</p>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-border">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-foreground-secondary hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={isLoading}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
