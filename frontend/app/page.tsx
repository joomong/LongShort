'use client';

import { useCallback, useEffect } from 'react';
import MainLayout from '@/components/layout/MainLayout';
import XmlEditor from '@/components/editor/XmlEditor';
import ControlButtons from '@/components/editor/ControlButtons';
import SettingsBar from '@/components/visualization/SettingsBar';
import BacktestMetrics from '@/components/visualization/BacktestMetrics';
import SignalDistribution from '@/components/visualization/SignalDistribution';
import SignalHeatmap from '@/components/visualization/SignalHeatmap';
import SignalChart from '@/components/visualization/SignalChart';
import SnapshotManager from '@/components/visualization/SnapshotManager';
import AlphaDetailView from '@/components/visualization/AlphaDetailView';
import SummaryMetrics from '@/components/multi-alpha/SummaryMetrics';
import AlphaRankingTable from '@/components/multi-alpha/AlphaRankingTable';
import SaveAlphaModal from '@/components/modals/SaveAlphaModal';
import LoadAlphaModal from '@/components/modals/LoadAlphaModal';
import { useBacktest } from '@/hooks/useBacktest';
import { useAlphaStore } from '@/store/alphaStore';

function LeftPanel() {
  const { viewMode, result } = useAlphaStore();

  // Multi-alpha mode (including GA partial results without combined)
  if (result?.combined || (result?.alphas && result.alphas.length > 1)) {
    return (
      <div className="flex flex-col h-full">
        <SettingsBar />
        <SummaryMetrics />
        <AlphaRankingTable />

        {/* Detail plot area (below the table) */}
        {viewMode === 'detail' && (
          <div className="border-t border-border">
            <AlphaDetailView />
          </div>
        )}
      </div>
    );
  }

  // Single-alpha mode
  return (
    <div className="flex flex-col h-full">
      <SettingsBar />
      <BacktestMetrics />
      {/* Plot area (only this region changes in detail mode) */}
      {viewMode === 'detail' ? (
        <AlphaDetailView />
      ) : (
        <>
          <SignalDistribution />
          <SignalHeatmap />
          <SignalChart />
          <SnapshotManager />
        </>
      )}
    </div>
  );
}

function RightPanel() {
  const { run, stop } = useBacktest();
  const { backtestError, setBacktestError, isRunning } = useAlphaStore();

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.shiftKey && e.key === 'Enter' && !isRunning) {
      e.preventDefault();
      run();
    }
  }, [run, isRunning]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div className="flex flex-col h-full">
      {backtestError && (
        <div className="flex items-center justify-between gap-2 px-3 py-2 bg-red-950/40 text-red-200 border-b border-red-900">
          <div className="text-sm truncate">{backtestError}</div>
          <button
            className="text-xs px-2 py-1 rounded bg-red-900/40 hover:bg-red-900/60"
            onClick={() => setBacktestError(null)}
            title="Dismiss"
          >
            Dismiss
          </button>
        </div>
      )}
      <ControlButtons onRun={run} onStop={stop} />
      <XmlEditor />
    </div>
  );
}

export default function Home() {
  useEffect(() => {
    useAlphaStore.getState().fetchDataRange();
    useAlphaStore.getState().fetchSymbols();
  }, []);

  return (
    <>
      <MainLayout
        leftPanel={<LeftPanel />}
        rightPanel={<RightPanel />}
      />
      <SaveAlphaModal />
      <LoadAlphaModal />
    </>
  );
}
