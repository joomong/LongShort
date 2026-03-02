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
const AlphaRankingTable = dynamic(() => import('@/components/multi-alpha/AlphaRankingTable'));
const SaveAlphaModal = dynamic(() => import('@/components/modals/SaveAlphaModal'));
const LoadAlphaModal = dynamic(() => import('@/components/modals/LoadAlphaModal'));

function SingleAlphaPanel() {
  return (
    <div className="flex flex-col h-full">
      <SettingsBar />
      <BacktestMetrics />
      <SignalDistribution />
      <SignalHeatmap />
      <SignalChart />
      <SnapshotManager />
    </div>
  );
}

function SingleAlphaDetailPanel() {
  return (
    <div className="flex flex-col h-full">
      <SettingsBar />
      <BacktestMetrics />
      <AlphaDetailView />
    </div>
  );
}

function MultiAlphaPanel() {
  const viewMode = useAlphaStore(s => s.viewMode);

  return (
    <div className="flex flex-col h-full">
      <SettingsBar />
      <div className={viewMode === 'detail' ? 'max-h-48 overflow-y-auto' : 'flex-1 min-h-0'}>
        <AlphaRankingTable />
      </div>
      {viewMode === 'detail' && (
        <div className="flex-1 min-h-0 overflow-y-auto border-t border-border">
          <AlphaDetailView />
        </div>
      )}
    </div>
  );
}

function LeftPanel() {
  const viewMode = useAlphaStore(s => s.viewMode);
  const isMulti = useAlphaStore(s => (s.result?.alphas?.length ?? 0) > 1);

  if (isMulti) {
    return <MultiAlphaPanel />;
  }

  if (viewMode === 'detail') {
    return <SingleAlphaDetailPanel />;
  }

  return <SingleAlphaPanel />;
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
        <div className="flex items-center justify-between gap-2 px-3 py-2 bg-metric-negative/10 text-metric-negative border-b border-metric-negative/20">
          <div className="text-sm truncate">{backtestError}</div>
          <button
            className="text-xs px-2 py-1 bg-red-900/40 hover:bg-red-900/60"
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
    useAlphaStore.getState().fetchConfig();
  }, []);

  return (
    <>
      <MainLayout
        leftPanel={<RightPanel />}
        rightPanel={<LeftPanel />}
        minLeftWidth={15}
      />
      <SaveAlphaModal />
      <LoadAlphaModal />
    </>
  );
}
