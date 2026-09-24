'use client';

import { useEffect } from 'react';
import dynamic from 'next/dynamic';
import MainLayout from '@/components/layout/MainLayout';
import ControlButtons, { AlphaStorageButtons } from '@/components/editor/ControlButtons';
import SettingsBar from '@/components/visualization/SettingsBar';
import AlphaXmlPanel from '@/components/visualization/AlphaXmlPanel';
import SignalPanelSelector from '@/components/visualization/SignalPanelSelector';
import { useBacktest } from '@/hooks/useBacktest';
import { useAlphaStore } from '@/store/alphaStore';

// Heavy components: lazy-loaded (uPlot ~50KB, Monaco ~300KB)
const LOADING_PLACEHOLDER = <div className="flex-1 min-h-0 bg-background-secondary animate-pulse" />;
const XmlEditor = dynamic(
  () => import('@/components/editor/XmlEditor'),
  { loading: () => LOADING_PLACEHOLDER }
);
const AlphaDetailView = dynamic(
  () => import('@/components/visualization/AlphaDetailView'),
  { loading: () => LOADING_PLACEHOLDER }
);
const AlphaRankingTable = dynamic(
  () => import('@/components/multi-alpha/AlphaRankingTable'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalDistribution = dynamic(
  () => import('@/components/visualization/SignalDistribution'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalHeatmap = dynamic(
  () => import('@/components/visualization/SignalHeatmap'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalChart = dynamic(
  () => import('@/components/visualization/SignalChart'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SnapshotManager = dynamic(
  () => import('@/components/visualization/SnapshotManager'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SaveAlphaModal = dynamic(() => import('@/components/modals/SaveAlphaModal'));
const LoadAlphaModal = dynamic(() => import('@/components/modals/LoadAlphaModal'));

// Modals: conditionally mounted so bundles load only when opened (bundle-conditional)
function Modals() {
  const isSaveOpen = useAlphaStore(s => s.isSaveModalOpen);
  const isLoadOpen = useAlphaStore(s => s.isLoadModalOpen);
  return (
    <>
      {isSaveOpen ? <SaveAlphaModal /> : null}
      {isLoadOpen ? <LoadAlphaModal /> : null}
    </>
  );
}

// Left panel: alpha name input + XML editor
function XmlPanel() {
  const alphaName = useAlphaStore(s => s.alphaName);
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-2 py-1 border-b border-[#555466] bg-[#353446]">
        <span className="text-[11px] text-[#c7c5d1] shrink-0">Alpha name:</span>
        <input
          type="text"
          value={alphaName}
          onChange={(e) => useAlphaStore.getState().setAlphaName(e.target.value)}
          placeholder="alpha name"
          className="flex-1 min-w-0 px-1 py-0.5 bg-transparent border border-transparent hover:border-[#777486] text-xs font-mono text-[#f0eff5] placeholder:text-[#9996ab] focus:outline-none focus:border-accent"
        />
        <AlphaStorageButtons />
      </div>
      <XmlEditor />
    </div>
  );
}

// Only single-alpha runs expose instrument-level signal analysis.
function ChartArea() {
  const isDetail = useAlphaStore(s => s.viewMode === 'detail');
  const alphaCount = useAlphaStore(s => s.result?.alphas.length ?? 0);

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-background">
      <div className={isDetail ? 'flex-1 min-h-0 flex flex-row' : 'hidden'} data-testid="alpha-detail">
        <div className="flex-1 min-h-0 min-w-0"><AlphaDetailView /></div>
        <div className="w-[28%] min-w-0 max-w-[440px] shrink-0 min-h-0"><AlphaXmlPanel /></div>
      </div>
      {alphaCount === 1 ? (
        <div className={isDetail ? 'hidden' : 'flex-1 min-h-0 flex flex-col overflow-y-auto'} data-testid="signal-analysis">
          <SignalPanelSelector />
          <SignalDistribution />
          <SignalHeatmap />
          <SignalChart />
          <SnapshotManager />
        </div>
      ) : !isDetail ? (
        <div className="flex-1 flex items-center justify-center text-xs text-foreground-secondary" data-testid="result-placeholder">
          {alphaCount > 1 ? 'Select an alpha row to view its performance and XML.' : ''}
        </div>
      ) : null}
    </div>
  );
}

// Results remain above both signal analysis and performance detail.
function MainContent() {
  const backtestError = useAlphaStore(s => s.backtestError);

  return (
    <div className="flex-1 flex flex-col min-w-0 h-full border-t-2 border-accent">
      {backtestError ? (
        <div className="flex items-center justify-between gap-2 px-3 py-2 bg-metric-negative/10 text-metric-negative border-b border-metric-negative/20">
          <div className="text-sm truncate">{backtestError}</div>
          <button
            className="text-xs px-2 py-1 bg-red-900/40 hover:bg-red-900/60"
            onClick={() => useAlphaStore.getState().setBacktestError(null)}
            title="Dismiss"
          >
            Dismiss
          </button>
        </div>
      ) : null}
      {/* Ranking table reserves five result rows; larger batches remain scrollable. */}
      <div className="overflow-auto shrink-0 max-h-[35%]">
        <AlphaRankingTable />
      </div>
      {/* Strong separator between table and chart */}
      <div className="h-px bg-border-strong shrink-0" />
      {/* Chart area fills remaining space */}
      <ChartArea />
    </div>
  );
}

// Module-level guard: fetchConfig fires once per page load (advanced-init-once)
let configFetched = false;

export default function Home() {
  const { run, runGA, stop } = useBacktest();

  useEffect(() => {
    if (!configFetched) {
      configFetched = true;
      useAlphaStore.getState().fetchConfig();
    }
  }, []);

  // Keyboard shortcut: isRunning read via getState() — not subscribed (rerender-defer-reads)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.shiftKey && e.key === 'Enter' && !useAlphaStore.getState().isRunning) {
        e.preventDefault();
        run();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [run]);

  return (
    <>
      <MainLayout
        topBar={<header className="flex flex-wrap items-end justify-start gap-2 bg-[#353446] border-b border-[#555466]">
          <SettingsBar />
          <div className="ml-auto self-start pt-1 pr-2">
            <ControlButtons onRun={run} onStop={stop} onRunGA={runGA} />
          </div>
        </header>}
        leftPanel={<XmlPanel />}
        rightPanel={<MainContent />}
        minLeftWidth={15}
      />
      <Modals />
    </>
  );
}
