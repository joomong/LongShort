'use client';

import { useEffect } from 'react';
import dynamic from 'next/dynamic';
import MainLayout from '@/components/layout/MainLayout';
import ControlButtons from '@/components/editor/ControlButtons';
import SettingsBar from '@/components/visualization/SettingsBar';
import { useBacktest } from '@/hooks/useBacktest';
import { useAlphaStore } from '@/store/alphaStore';

// Heavy components: lazy-loaded (Recharts ~200KB, Monaco ~300KB)
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

// Grid background style — crosshatch visible in empty chart area
const GRID_BG: React.CSSProperties = {
  backgroundImage:
    'linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px),' +
    'linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px)',
  backgroundSize: '48px 48px',
};

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
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-background-secondary panel-inset">
        <span className="text-[11px] text-foreground-muted font-semibold uppercase tracking-wide shrink-0">Name</span>
        <input
          type="text"
          value={alphaName}
          onChange={(e) => useAlphaStore.getState().setAlphaName(e.target.value)}
          placeholder="alpha name"
          className="flex-1 px-2 py-0.5 bg-background-elevated border border-border text-xs font-mono text-foreground placeholder:text-foreground-muted focus:outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>
      <XmlEditor />
    </div>
  );
}

// Chart area: page-switch between charts and detail view
function ChartArea() {
  const viewMode = useAlphaStore(s => s.viewMode);

  return (
    <div className="flex-1 min-h-0 flex flex-col" style={GRID_BG}>
      {viewMode === 'detail' ? (
        <div className="flex-1 min-h-0">
          <AlphaDetailView />
        </div>
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

// Right panel content: settings + ranking table + chart area (no buttons — they're in sidebar)
function MainContent() {
  const backtestError = useAlphaStore(s => s.backtestError);
  const viewMode = useAlphaStore(s => s.viewMode);

  return (
    <div className="flex-1 flex flex-col min-w-0 h-full">
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
      <SettingsBar />
      {/* Ranking table: bounded height, scrollable */}
      <div className={
        'overflow-y-auto min-h-[180px] ' +
        (viewMode === 'detail' ? 'max-h-48' : 'max-h-[40%]')
      }>
        <AlphaRankingTable />
      </div>
      {/* Strong separator between table and chart */}
      <div className="h-px bg-border-strong shrink-0" />
      {/* Chart area fills remaining space */}
      <ChartArea />
    </div>
  );
}

// Right panel: main content + vertical button sidebar on the FAR RIGHT of the screen
function MainPanel({ onRun, onStop }: { onRun: () => void; onStop: () => void }) {
  return (
    <div className="flex flex-row h-full">
      <MainContent />
      {/* Vertical button strip — positioned at the screen's far right edge */}
      <ControlButtons onRun={onRun} onStop={onStop} vertical />
    </div>
  );
}

// Module-level guard: fetchConfig fires once per page load (advanced-init-once)
let configFetched = false;

export default function Home() {
  const { run, stop } = useBacktest();

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
        leftPanel={<XmlPanel />}
        rightPanel={<MainPanel onRun={run} onStop={stop} />}
        minLeftWidth={15}
      />
      <Modals />
    </>
  );
}
