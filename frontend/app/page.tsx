'use client';

import { useCallback, useEffect } from 'react';
import dynamic from 'next/dynamic';
import MainLayout from '@/components/layout/MainLayout';
import ControlButtons from '@/components/editor/ControlButtons';
import SettingsBar from '@/components/visualization/SettingsBar';
import { useBacktest } from '@/hooks/useBacktest';
import { useAlphaStore } from '@/store/alphaStore';

// Heavy components: lazy-loaded (Recharts ~200KB, Monaco ~300KB)
const XmlEditor = dynamic(() => import('@/components/editor/XmlEditor'));
const BacktestMetrics = dynamic(() => import('@/components/visualization/BacktestMetrics'));
const SignalDistribution = dynamic(() => import('@/components/visualization/SignalDistribution'));
const SignalHeatmap = dynamic(() => import('@/components/visualization/SignalHeatmap'));
const SignalChart = dynamic(() => import('@/components/visualization/SignalChart'));
const SnapshotManager = dynamic(() => import('@/components/visualization/SnapshotManager'));
const AlphaDetailView = dynamic(() => import('@/components/visualization/AlphaDetailView'));
const SummaryMetrics = dynamic(() => import('@/components/multi-alpha/SummaryMetrics'));
const AlphaRankingTable = dynamic(() => import('@/components/multi-alpha/AlphaRankingTable'));
const SaveAlphaModal = dynamic(() => import('@/components/modals/SaveAlphaModal'));
const LoadAlphaModal = dynamic(() => import('@/components/modals/LoadAlphaModal'));

function LeftPanel() {
  const viewMode = useAlphaStore(s => s.viewMode);
  const isMulti = useAlphaStore(s =>
    !!(s.result?.combined || (s.result?.alphas && s.result.alphas.length > 1))
  );

  if (isMulti) {
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
  const backtestError = useAlphaStore(s => s.backtestError);
  const setBacktestError = useAlphaStore(s => s.setBacktestError);
  const isRunning = useAlphaStore(s => s.isRunning);

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
