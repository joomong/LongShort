'use client';

import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';

interface ControlButtonsProps {
  onRun: () => void;
  onStop: () => void;
  onRunGA: () => void;
  vertical?: boolean;
}

export default function ControlButtons({ onRun, onStop, onRunGA, vertical = false }: ControlButtonsProps) {
  const isRunning = useAlphaStore(s => s.isRunning);
  const gaProgress = useAlphaStore(s => s.gaProgress);
  const setIsSaveModalOpen = useAlphaStore(s => s.setIsSaveModalOpen);
  const setIsLoadModalOpen = useAlphaStore(s => s.setIsLoadModalOpen);
  const isSaveDisabled = useAlphaStore(s =>
    !s.result || (s.viewMode === 'multi' && s.selectedAlphaIndices.length === 0)
  );
  const isMultiMode = useAlphaStore(s => s.viewMode === 'multi');
  const setResult = useAlphaStore(s => s.setResult);
  const hasResult = useAlphaStore(s => s.result !== null);
  const { theme, toggleTheme } = useThemeStore();

  const divider = vertical
    ? <div className="h-px w-6 bg-border my-0.5" />
    : <div className="w-px h-6 bg-border mx-1" />;

  const containerClass = vertical
    ? 'flex flex-col items-center gap-1.5 py-2 px-1 bg-background-secondary border-l border-border w-11 shrink-0 panel-inset'
    : 'flex items-center gap-2 p-2 bg-background-secondary border-b border-border panel-inset';

  return (
    <div className={containerClass}>
      {/* Run Button */}
      <button
        onClick={onRun}
        disabled={isRunning}
        className={`flex items-center justify-center w-9 h-9 transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
          isRunning
            ? 'bg-background-elevated text-foreground-muted cursor-not-allowed'
            : 'bg-accent hover:bg-accent-hover text-white'
        }`}
        title="Run Backtest (Shift+Enter)"
        aria-label="Run Backtest (Shift+Enter)"
      >
        <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
          <path d="M6.3 2.841A1.5 1.5 0 004 4.11v11.78a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
        </svg>
      </button>

      <button onClick={onRunGA} disabled={isRunning} aria-label="Run GA" title="Run GA"
        className="flex items-center justify-center w-9 h-9 bg-background-elevated hover:bg-border-hover text-foreground font-mono text-xs disabled:opacity-40 focus-visible:ring-2 focus-visible:ring-accent">
        GA
      </button>

      {/* Stop Button */}
      <button
        onClick={onStop}
        disabled={!isRunning}
        className={`flex items-center justify-center w-9 h-9 transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
          !isRunning
            ? 'bg-background-elevated text-foreground-muted cursor-not-allowed'
            : 'bg-metric-negative hover:bg-red-400 text-white'
        }`}
        title="Stop"
        aria-label="Stop"
      >
        <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
          <rect x="5" y="5" width="10" height="10" rx="1" />
        </svg>
      </button>

      {divider}

      {/* Pin Signal Button (save signal) */}
      <button
        onClick={() => setIsSaveModalOpen(true)}
        disabled={isSaveDisabled}
        className={`flex items-center justify-center w-9 h-9 transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
          isSaveDisabled
            ? 'bg-background-elevated text-foreground-muted cursor-not-allowed'
            : 'bg-background-elevated hover:bg-border-hover text-foreground-secondary hover:text-foreground'
        }`}
        title={isSaveDisabled && isMultiMode ? 'Select alphas first' : 'Pin Signal'}
        aria-label={isSaveDisabled && isMultiMode ? 'Select alphas first' : 'Pin Signal'}
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" />
        </svg>
      </button>

      {/* Load Button */}
      <button
        onClick={() => setIsLoadModalOpen(true)}
        className="flex items-center justify-center w-9 h-9 bg-background-elevated hover:bg-border-hover text-foreground-secondary hover:text-foreground transition-colors focus-visible:ring-2 focus-visible:ring-accent"
        title="Load Alpha"
        aria-label="Load Alpha"
      >
        <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
        </svg>
      </button>

      {divider}

      {/* Theme Toggle Button */}
      <button
        onClick={toggleTheme}
        className="flex items-center justify-center w-9 h-9 bg-background-elevated hover:bg-border-hover transition-colors focus-visible:ring-2 focus-visible:ring-accent"
        title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
      >
        {theme === 'dark' ? (
          <svg className="w-5 h-5 text-yellow-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
          </svg>
        ) : (
          <svg className="w-5 h-5 text-foreground-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
          </svg>
        )}
      </button>

      {divider}

      {/* Clear Button */}
      <button
        onClick={() => setResult(null)}
        disabled={!hasResult || isRunning}
        className={`flex items-center justify-center w-9 h-9 transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
          !hasResult || isRunning
            ? 'bg-background-elevated text-foreground-muted cursor-not-allowed'
            : 'bg-background-elevated hover:bg-border-hover text-foreground-secondary hover:text-foreground'
        }`}
        title="Clear Results"
        aria-label="Clear Results"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      {/* Running Indicator */}
      {isRunning ? (
        <div className={`flex ${vertical ? 'flex-col mt-auto items-center' : 'items-center gap-2 ml-auto'} text-accent`}>
          <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
          </svg>
          {!vertical ? (
            <span className="text-xs font-mono">{gaProgress ? 'GA\u2026' : 'Running\u2026'}</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
