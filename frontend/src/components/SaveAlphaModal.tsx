'use client';

import { useState } from 'react';
import { useRunStore } from '@/runStore';
import { AlphaExistsError, saveAlpha } from '@/api';
import type { SaveAlphaRequest, SavedAlphaLevel, SavedAlphaMetadata } from '@/types';

const LEVELS: { value: SavedAlphaLevel; label: string }[] = [
  { value: 'xml', label: 'XML only' },
  { value: 'results', label: 'XML + results' },
  { value: 'positions', label: 'XML + results + positions' },
];

export default function SaveAlphaModal() {
  const isSaveModalOpen = useRunStore(s => s.isSaveModalOpen);
  const setIsSaveModalOpen = useRunStore(s => s.setIsSaveModalOpen);
  const result = useRunStore(s => s.result);
  const xmlCode = useRunStore(s => s.xmlCode);
  const settings = useRunStore(s => s.settings);
  const config = useRunStore(s => s.config);
  const isRunning = useRunStore(s => s.isRunning);
  const name = useRunStore(s => s.alphaName).trim();
  const [level, setLevel] = useState<SavedAlphaLevel>('xml');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  if (!isSaveModalOpen) return null;

  // Rows = every successful candidate of the current result, in candidate (batch) order.
  const rows = (result?.alphas ?? []).map(alpha => ({ alpha }))
    .filter(({ alpha }) => alpha.status === 'completed' && !!alpha.xml)
    .sort((a, b) => a.alpha.id - b.alpha.id)
    .map(({ alpha }) => ({ id: alpha.id, round: alpha.round ?? 0, xml: alpha.xml! }));
  const resultsBlocked = isRunning ? 'A run is in progress.'
    : !result || rows.length === 0 ? 'No finished result with successful rows.'
    : result.context.universe && config && result.context.universe !== config.defaultUniverse && !config.universes.includes(result.context.universe) ? 'The result universe is unavailable. Start a new run before saving recomputed results.'
    : result.sourceXml !== xmlCode ? 'Editor XML differs from the XML that produced the result.'
    : '';
  const effectiveLevel: SavedAlphaLevel = resultsBlocked ? 'xml' : level;

  const metadataFor = (target: SavedAlphaLevel): SavedAlphaMetadata => {
    const source = target === 'xml' ? settings : result!.context;
    return { simStart: source.simStart, simEnd: source.simEnd || config?.simEnd || '',
      isStart: source.isStart, osStart: source.osStart,
      universe: source.universe || config?.defaultUniverse || '', executionPrice: source.executionPrice, threads: source.threads };
  };

  const handleSave = async () => {
    if (!name) return;
    if (!xmlCode.trim()) {
      setError('Editor XML is empty.');
      return;
    }
    const request: SaveAlphaRequest = { name, xml: xmlCode, level: effectiveLevel, metadata: metadataFor(effectiveLevel) };
    if (effectiveLevel !== 'xml') request.rows = rows;

    setIsLoading(true);
    setError('');
    try {
      await saveAlpha(request);
      setLevel('xml');
      setIsSaveModalOpen(false);
    } catch (err) {
      setError(err instanceof AlphaExistsError ? `"${request.name}" already exists. Change the Alpha name field.`
        : err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setIsLoading(false);
    }
  };

  const handleClose = () => {
    if (isLoading) return;
    setError('');
    setLevel('xml');
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
          {/* Name comes from the toolbar "Alpha name" field */}
          {name ? (
            <p className="text-sm text-foreground" data-testid="save-alpha-name">Name: <span className="font-mono">{name}</span></p>
          ) : (
            <p className="text-sm text-metric-negative" data-testid="save-alpha-name">Enter a name in the Alpha name field before saving.</p>
          )}

          {/* Save level */}
          <fieldset>
            <legend className="block text-sm text-foreground-secondary mb-2">Save level</legend>
            <div className="space-y-1">
              {LEVELS.map(option => {
                const disabled = option.value !== 'xml' && !!resultsBlocked;
                return (
                  <label key={option.value} className={`flex items-center gap-2 text-sm ${disabled ? 'text-foreground-muted cursor-not-allowed' : 'text-foreground cursor-pointer'}`}>
                    <input type="radio" name="save-alpha-level" value={option.value} checked={effectiveLevel === option.value}
                      disabled={disabled || isLoading} onChange={() => setLevel(option.value)} />
                    {option.label}
                  </label>
                );
              })}
            </div>
            {resultsBlocked ? <p className="mt-1 text-xs text-foreground-muted" data-testid="save-level-reason">Results unavailable: {resultsBlocked}</p>
              : effectiveLevel !== 'xml' ? <p className="mt-1 text-xs text-foreground-muted">{rows.length} row(s); the engine recomputes them, which can take a while.</p> : null}
          </fieldset>

          {config?.alphasDir ? (
            <p className="text-xs text-foreground-muted font-mono break-all">Directory: {config.alphasDir}</p>
          ) : null}

          {/* Error */}
          {error && (
            <p role="alert" className="text-sm text-metric-negative">{error}</p>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-border">
          <button
            onClick={handleClose}
            disabled={isLoading}
            className="px-4 py-2 text-foreground-secondary hover:text-foreground transition-colors focus-visible:ring-2 focus-visible:ring-accent"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={isLoading || !name || !xmlCode.trim()}
            className="px-4 py-2 bg-accent hover:bg-accent-hover text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-accent"
          >
            {isLoading ? 'Saving\u2026' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
