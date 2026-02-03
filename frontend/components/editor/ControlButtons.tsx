'use client';

import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';

interface ControlButtonsProps {
  onRun: () => void;
  onStop: () => void;
}

export default function ControlButtons({ onRun, onStop }: ControlButtonsProps) {
  const isRunning = useAlphaStore(s => s.isRunning);
  const gaProgress = useAlphaStore(s => s.gaProgress);
  const setIsSaveModalOpen = useAlphaStore(s => s.setIsSaveModalOpen);
  const setIsLoadModalOpen = useAlphaStore(s => s.setIsLoadModalOpen);
  const isSaveDisabled = useAlphaStore(s =>
    !s.result || (s.viewMode === 'multi' && s.selectedAlphaIndices.length === 0)
  );
  const isMultiMode = useAlphaStore(s => s.viewMode === 'multi');
  const { theme, toggleTheme } = useThemeStore();

  return (
    <div className="flex items-center gap-2 p-2 bg-background-tertiary border-b border-border">
      {/* Run Button */}
      <button
        onClick={onRun}
        disabled={isRunning}
        className={`flex items-center justify-center w-10 h-10 rounded transition-colors ${
          isRunning
            ? 'bg-foreground-muted cursor-not-allowed'
            : 'bg-green-600 hover:bg-green-500'
        }`}
        title="Run Backtest (Shift+Enter)"
      >
        <svg
          className="w-5 h-5 text-white"
          fill="currentColor"
          viewBox="0 0 20 20"
        >
          <path d="M6.3 2.841A1.5 1.5 0 004 4.11v11.78a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
        </svg>
      </button>

      {/* Stop Button */}
      <button
        onClick={onStop}
        disabled={!isRunning}
        className={`flex items-center justify-center w-10 h-10 rounded transition-colors ${
          !isRunning
            ? 'bg-foreground-muted cursor-not-allowed'
            : 'bg-red-600 hover:bg-red-500'
        }`}
        title="Stop"
      >
        <svg
          className="w-5 h-5 text-white"
          fill="currentColor"
          viewBox="0 0 20 20"
        >
          <rect x="5" y="5" width="10" height="10" rx="1" />
        </svg>
      </button>

      <div className="w-px h-8 bg-border mx-2" />

      {/* Save Button */}
      <button
        onClick={() => setIsSaveModalOpen(true)}
        disabled={isSaveDisabled}
        className={`flex items-center justify-center w-10 h-10 rounded transition-colors ${
          isSaveDisabled
            ? 'bg-foreground-muted cursor-not-allowed'
            : 'bg-blue-600 hover:bg-blue-500'
        }`}
        title={isSaveDisabled && isMultiMode ? 'Select alphas first' : 'Save Alpha'}
      >
        <svg
          className="w-5 h-5 text-white"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"
          />
        </svg>
      </button>

      {/* Load Button */}
      <button
        onClick={() => setIsLoadModalOpen(true)}
        className="flex items-center justify-center w-10 h-10 rounded bg-purple-600 hover:bg-purple-500 transition-colors"
        title="Load Alpha"
      >
        <svg
          className="w-5 h-5 text-white"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
          />
        </svg>
      </button>

      <div className="w-px h-8 bg-border mx-2" />

      {/* Theme Toggle Button */}
      <button
        onClick={toggleTheme}
        className="flex items-center justify-center w-10 h-10 rounded bg-background-elevated hover:bg-border-hover transition-colors"
        title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
      >
        {theme === 'dark' ? (
          <svg className="w-5 h-5 text-yellow-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
          </svg>
        ) : (
          <svg className="w-5 h-5 text-foreground-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
          </svg>
        )}
      </button>

      {/* Running Indicator */}
      {isRunning && (
        <div className="ml-auto flex items-center gap-2 text-yellow-400">
          <svg
            className="w-4 h-4 animate-spin"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
            />
          </svg>
          <span className="text-sm">{gaProgress ? 'GA...' : 'Running...'}</span>
        </div>
      )}
    </div>
  );
}
