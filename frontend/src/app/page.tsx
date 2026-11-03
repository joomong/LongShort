'use client';

import { startTransition, useEffect } from 'react';
import dynamic from 'next/dynamic';
import MainLayout from '@/components/MainLayout';
import RunControls, { AlphaStorageButtons } from '@/components/RunControls';
import RunSettingsBar from '@/components/RunSettingsBar';
import AlphaXmlPanel from '@/components/AlphaXmlPanel';
import ResultsTabs from '@/components/ResultsTabs';
import { useAlphaSearch } from '@/useAlphaSearch';
import { useResultData } from '@/useResultData';
import { canShortcutRun, useRunStore } from '@/runStore';

// Heavy components: lazy-loaded (uPlot ~50KB, Monaco ~300KB)
const LOADING_PLACEHOLDER = <div className="flex-1 min-h-0 bg-background-secondary animate-pulse" />;
const XmlEditor = dynamic(
  () => import('@/components/XmlEditor'),
  { loading: () => LOADING_PLACEHOLDER }
);
const PerformanceReport = dynamic(
  () => import('@/components/PerformanceReport'),
  { loading: () => LOADING_PLACEHOLDER }
);
const ResultsTable = dynamic(
  () => import('@/components/ResultsTable'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalDistribution = dynamic(
  () => import('@/components/SignalDistribution'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalHeatmap = dynamic(
  () => import('@/components/SignalHeatmap'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SignalChart = dynamic(
  () => import('@/components/SignalChart'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SnapshotManager = dynamic(
  () => import('@/components/SnapshotManager'),
  { loading: () => LOADING_PLACEHOLDER }
);
const SaveAlphaModal = dynamic(() => import('@/components/SaveAlphaModal'));
const LoadAlphaModal = dynamic(() => import('@/components/LoadAlphaModal'));

// Modals: conditionally mounted so bundles load only when opened (bundle-conditional)
function Modals() {
  const isSaveOpen = useRunStore(s => s.isSaveModalOpen);
  const isLoadOpen = useRunStore(s => s.isLoadModalOpen);
  return (
    <>
      {isSaveOpen ? <SaveAlphaModal /> : null}
      {isLoadOpen ? <LoadAlphaModal /> : null}
    </>
  );
}

// Left panel: alpha name input + XML editor
function XmlPanel() {
  const alphaName = useRunStore(s => s.alphaName);
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-2 py-1 border-b border-[#555466] bg-[#353446]">
        <span className="text-[11px] text-[#c7c5d1] shrink-0">Alpha name:</span>
        <input
          type="text"
          value={alphaName}
          onChange={(e) => useRunStore.getState().setAlphaName(e.target.value)}
          placeholder="alpha name"
          className="flex-1 min-w-0 px-1 py-0.5 bg-transparent border border-transparent hover:border-[#777486] text-xs font-mono text-[#f0eff5] placeholder:text-[#9996ab] focus:outline-none focus:border-accent"
        />
        <AlphaStorageButtons />
      </div>
      <XmlEditor />
    </div>
  );
}

// Reclicking the selected row does nothing, so a finished single-alpha result switches views here.
const SINGLE_VIEWS = [
  { id: 'signals', label: 'Signals', select: () => useRunStore.getState().exitDetailView() },
  { id: 'performance', label: 'Performance', select: () => useRunStore.getState().enterDetailView(0) },
] as const;

function SingleAlphaViewTabs({ isDetail }: { isDetail: boolean }) {
  const activeIndex = isDetail ? 1 : 0;
  const choose = (index: number, focus = false) => {
    startTransition(SINGLE_VIEWS[index].select);
    if (focus) document.getElementById(`single-alpha-tab-${SINGLE_VIEWS[index].id}`)?.focus();
  };
  return (
    <div role="tablist" aria-label="Single alpha view" className="flex shrink-0 items-center gap-2 px-2 border-b border-border bg-background-secondary">
      {SINGLE_VIEWS.map((view, index) => (
        <button key={view.id} role="tab" id={`single-alpha-tab-${view.id}`} aria-controls={`single-alpha-${view.id}`}
          aria-selected={index === activeIndex} tabIndex={index === activeIndex ? 0 : -1}
          onClick={() => choose(index)}
          onKeyDown={event => {
            const next = event.key === 'ArrowLeft' || event.key === 'ArrowRight' ? 1 - index
              : event.key === 'Home' ? 0 : event.key === 'End' ? SINGLE_VIEWS.length - 1 : -1;
            if (next < 0) return;
            event.preventDefault();
            choose(next, true);
          }}
          className={'shrink-0 text-[10px] py-1 border-b-2 whitespace-nowrap transition-colors ' +
            (index === activeIndex ? 'border-accent text-foreground font-semibold' : 'border-transparent text-foreground-secondary hover:text-foreground')}>
          {view.label}
        </button>
      ))}
    </div>
  );
}

// Only single-alpha runs expose instrument-level signal analysis.
function ChartArea() {
  const isDetail = useRunStore(s => s.viewMode === 'detail');
  const alphaCount = useRunStore(s => s.result?.alphas.length ?? 0);
  const signalRequest = useRunStore(s => s.signalRequest);
  const views = useRunStore(s => (!s.gaSession || s.gaFinished) && s.result?.alphas.length === 1 && !s.result.alphas[0].error);
  const panel = (id: string) => views ? { role: 'tabpanel', id: `single-alpha-${id}`, 'aria-labelledby': `single-alpha-tab-${id}` } : {};

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-background">
      {views ? <SingleAlphaViewTabs isDetail={isDetail} /> : null}
      <div className={isDetail ? 'flex-1 min-h-0 flex flex-row' : 'hidden'} data-testid="alpha-detail" {...panel('performance')}>
        <div className="flex-1 min-h-0 min-w-0"><PerformanceReport /></div>
        <div className="w-[28%] min-w-0 max-w-[440px] shrink-0 min-h-0"><AlphaXmlPanel /></div>
      </div>
      {alphaCount === 1 ? (
        <div className={isDetail ? 'hidden' : 'flex-1 min-h-0 flex flex-col overflow-y-auto'} data-testid="signal-analysis" {...panel('signals')}>
          {signalRequest.status === 'loading' ? <div role="status" className="px-2 py-1 text-xs text-foreground-muted">Loading signals…</div> : null}
          {signalRequest.status === 'error' ? <div role="alert" className="px-2 py-1 text-xs text-metric-negative">{signalRequest.error}
            <button onClick={() => useRunStore.getState().retryResultData('signals')} className="ml-2 underline">Retry signals</button>
          </div> : null}
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
  const backtestError = useRunStore(s => s.backtestError);
  const configRequest = useRunStore(s => s.configRequest);

  return (
    <div className="flex-1 flex flex-col min-w-0 h-full">
      <ResultsTabs />
      {configRequest.status === 'error' ? <div role="alert" className="px-3 py-2 text-sm text-metric-negative">{configRequest.error}
        <button onClick={() => useRunStore.getState().retryConfig()} className="ml-2 underline">Retry configuration</button>
      </div> : null}
      {backtestError ? (
        <div className="flex items-center justify-between gap-2 px-3 py-2 bg-metric-negative/10 text-metric-negative border-b border-metric-negative/20">
          <div className="text-sm truncate">{backtestError}</div>
          <button
            className="text-xs px-2 py-1 bg-red-900/40 hover:bg-red-900/60"
            onClick={() => useRunStore.getState().setBacktestError(null)}
            title="Dismiss"
          >
            Dismiss
          </button>
        </div>
      ) : null}
      {/* Ranking table reserves five result rows; larger batches remain scrollable. */}
      <div id="alpha-results-panel" role="tabpanel" className="overflow-auto shrink-0 max-h-[35%]">
        <ResultsTable />
      </div>
      {/* Strong separator between table and chart */}
      <div className="h-px bg-border-strong shrink-0" />
      {/* Chart area fills remaining space */}
      <ChartArea />
    </div>
  );
}

export default function Home() {
  const { startSearch, stopSearch } = useAlphaSearch();
  useResultData();

  // Keyboard shortcut: isRunning read via getState() — not subscribed (rerender-defer-reads)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.shiftKey && e.key === 'Enter' && canShortcutRun(useRunStore.getState())) {
        e.preventDefault();
        startSearch();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [startSearch]);

  return (
    <>
      <MainLayout
        topBar={<header className="flex flex-wrap items-end justify-start gap-2 bg-[#353446] border-b border-[#555466]">
          <RunSettingsBar />
          <div className="ml-auto self-start pt-1 pr-2">
            <RunControls onRun={startSearch} onStop={stopSearch} />
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
