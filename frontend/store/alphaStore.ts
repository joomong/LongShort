import { create } from 'zustand';
import type { BacktestRunResponse, BacktestSettings, ViewMode, PinnedSnapshot, GAProgress, StarConfigResponse, SignalPayload, GARound, BacktestAlphaResult } from '@/types';
import { getConfig } from '@/lib/api';
import { selectSignalCell, selectSignalSource } from '@/lib/signalSelection';
import { gaCombinedGroup, pendingGACombined } from '@/lib/gaCombined';

interface AlphaState {
  // XML Editor
  xmlCode: string;
  setXmlCode: (code: string) => void;

  // Alpha Name
  alphaName: string;
  setAlphaName: (name: string) => void;

  // UI Error
  backtestError: string | null;
  setBacktestError: (message: string | null) => void;

  // Backtest Settings
  settings: BacktestSettings;
  setSettings: (settings: Partial<BacktestSettings>) => void;

  // Backtest Result (single endpoint: /api/backtest/run)
  result: BacktestRunResponse | null;
  setResult: (result: BacktestRunResponse | null) => void;
  isRunning: boolean;
  setIsRunning: (running: boolean) => void;

  resultRevision: number;
  signalCacheOrder: number[];
  selectedSignalAlphaIndex: number | null;
  selectSignalAlpha: (index: number) => void;
  cacheAlphaSignals: (index: number, signals: SignalPayload, revision: number) => void;

  // Selected Cell (for chart)
  selectedSymbol: string | null;
  selectedTimestamp: string | null;
  setSelectedCell: (symbol: string | null, timestamp: string | null) => void;

  // View Mode
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;

  // Detail View
  selectedAlphaIndex: number | null;
  enterDetailView: (index: number) => void;
  exitDetailView: () => void;

  // Alpha Selection (multi-alpha save)
  selectedAlphaIndices: number[];
  selectedAlphaIndexSet: Set<number>; // O(1) .has() lookup (js-set-map-lookups)
  toggleAlphaSelection: (index: number) => void;
  toggleAllAlphaSelection: () => void;
  clearAlphaSelection: () => void;

  // Modal States
  isSaveModalOpen: boolean;
  setIsSaveModalOpen: (open: boolean) => void;
  isLoadModalOpen: boolean;
  setIsLoadModalOpen: (open: boolean) => void;

  // Config
  config: StarConfigResponse | null;
  fetchConfig: () => Promise<void>;

  // GA Progress
  gaProgress: GAProgress | null;
  setGAProgress: (progress: GAProgress | null) => void;

  gaSession: boolean;
  /** Search completed or stopped; a later stopGA is a no-op. */
  gaFinished: boolean;
  gaRounds: GARound[];
  activeResultTab: 'stats' | number;
  gaCombinedKey: string;
  gaCombinedCache: Record<string, BacktestAlphaResult>;
  cacheGACombined: (key: string, combined: BacktestAlphaResult, revision: number) => void;
  beginGA: (settings: NonNullable<BacktestRunResponse['runSettings']>, sourceXml?: string) => void;
  /** Show a saved result as a finished GA (rows/detail/batch read from saved files). */
  loadSavedResult: (result: BacktestRunResponse) => void;
  startGARound: (round: number) => void;
  appendGACandidate: (alpha: BacktestAlphaResult) => void;
  endGARound: (round: number) => void;
  finishGA: () => void;
  stopGA: () => void;
  clearGA: () => void;
  selectResultTab: (tab: 'stats' | number) => void;
  cacheGADetail: (index: number, detail: Partial<BacktestAlphaResult>, revision: number) => void;

  // Snapshots
  pinnedSnapshots: PinnedSnapshot[];
  addSnapshot: (snapshot: PinnedSnapshot) => void;
  removeSnapshot: (id: string) => void;
  updateSnapshotName: (id: string, name: string) => void;
}

export const MAX_CACHED_SIGNAL_ALPHAS = 5;

/** The global Shift+Enter shortcut runs only when idle and no save/load dialog is open. */
export function canShortcutRun(s: Pick<AlphaState, 'isRunning' | 'isSaveModalOpen' | 'isLoadModalOpen'>) {
  return !s.isRunning && !s.isSaveModalOpen && !s.isLoadModalOpen;
}

function combinedView(state: AlphaState, alphas = state.result?.alphas ?? [], tab = state.activeResultTab) {
  const { key } = gaCombinedGroup(alphas, tab);
  return {
    gaCombinedKey: key,
    result: state.result ? { ...state.result, alphas, combined: key ? state.gaCombinedCache[key] ??
      (state.gaCombinedKey === key ? state.result.combined : null) ?? pendingGACombined() : null } : null,
  };
}

/** A finished single-alpha result opens its signal analysis; performance detail stays one tab away. */
function singleAlphaSignals(state: AlphaState) {
  const alphas = state.result?.alphas ?? [];
  return alphas.length === 1 && !alphas[0].error && state.viewMode === 'detail' && state.selectedAlphaIndex === 0
    ? { viewMode: 'single' as const, selectedAlphaIndex: null } : {};
}

const defaultSettings: BacktestSettings = {
  simStart: '2016-01-01',
  simEnd: '',
  isStart: '2016-01-01',
  osStart: '2024-01-01',
  executionPrice: 'close',
  universe: '',
  nJobs: 20,
  gaPopulation: 50,
  gaFirstSearch: 200,
  gaStopRound: 40,
  gaStopAlphas: 400,
};

const defaultXml = `<data expr="close" delay="1"/>`;

function resetSplit(s: BacktestSettings, from: string, to: string) {
  const start = new Date(from + 'T12:00:00Z');
  const end = new Date(to + 'T12:00:00Z');
  const totalDays = Math.round((end.getTime() - start.getTime()) / 86400000);
  start.setUTCDate(start.getUTCDate() + Math.max(1, Math.round(totalDays * 0.7)));
  s.simStart = from;
  s.isStart = from;
  s.osStart = start.toISOString().slice(0, 10);
}

/** Preserve warmup and valid splits inside the inclusive simulation window. */
export function enforceSettingsDates(
  s: BacktestSettings,
  minDate?: string,
  maxDate?: string,
): BacktestSettings {
  const r = { ...s };
  const clamp = (d: string) => {
    if (minDate && d < minDate) return minDate;
    if (maxDate && d > maxDate) return maxDate;
    return d;
  };
  const nextDay = (d: string) => {
    const dt = new Date(d + 'T12:00:00Z');
    dt.setUTCDate(dt.getUTCDate() + 1);
    return dt.toISOString().slice(0, 10);
  };

  r.simStart = clamp(r.simStart);
  r.simEnd = r.simEnd ? clamp(r.simEnd) : (maxDate ?? '');
  if (r.simEnd && r.simStart > r.simEnd) r.simStart = r.simEnd;
  if (r.simEnd && (r.isStart > r.simEnd || r.osStart > r.simEnd) &&
      !(r.simStart === r.simEnd && r.isStart === r.simStart && r.osStart === nextDay(r.simEnd))) {
    resetSplit(r, r.simStart, r.simEnd);
  }
  r.isStart = clamp(r.isStart);
  r.osStart = r.simStart === r.simEnd ? nextDay(r.simStart) : clamp(r.osStart);

  // Enforce simStart <= isStart < osStart
  if (r.simStart > r.isStart) r.isStart = r.simStart;
  if (r.isStart >= r.osStart) {
    r.osStart = nextDay(r.isStart);
    if (r.simEnd && r.osStart > r.simEnd && r.simStart !== r.simEnd) resetSplit(r, r.simStart, r.simEnd);
  }

  return r;
}

export const useAlphaStore = create<AlphaState>((set) => ({
  // XML Editor
  xmlCode: defaultXml,
  setXmlCode: (code) =>
    set((state) => (state.xmlCode === code ? state : { xmlCode: code })),

  // Alpha Name
  alphaName: '',
  setAlphaName: (name) => set({ alphaName: name }),

  // UI Error
  backtestError: null,
  setBacktestError: (message) => set({ backtestError: message }),

  // Backtest Settings
  settings: defaultSettings,
  setSettings: (newSettings) =>
    set((state) => ({
      settings: enforceSettingsDates(
        { ...state.settings, ...newSettings },
        state.config?.simStart,
        state.config?.simEnd,
      ),
    })),

  // Backtest Result
  result: null,
  setResult: (result) =>
    set((state) => {
      const isMulti = (result?.alphas?.length ?? 0) > 1;
      const baseMode = isMulti ? 'multi' : 'single';
      const preserveDetail = state.gaProgress && state.viewMode === 'detail';
      const selectedAlphaIndex = preserveDetail && state.selectedAlphaIndex !== null &&
        (state.selectedAlphaIndex === -1 ? !!result?.combined : !!result?.alphas[state.selectedAlphaIndex])
        ? state.selectedAlphaIndex : isMulti ? (result?.combined ? -1 : 0) : null;
      const selectedSignalAlphaIndex = selectedAlphaIndex ?? (result?.combined ? -1 : result?.alphas.length ? 0 : null);
      return {
        result,
        resultRevision: state.resultRevision + 1,
        signalCacheOrder: result?.alphas.flatMap((alpha, index) => alpha.heatmap ? [index] : []) ?? [],
        viewMode: selectedAlphaIndex !== null ? 'detail' : baseMode,
        selectedAlphaIndex,
        selectedSignalAlphaIndex,
        ...selectSignalCell(selectSignalSource({ result, selectedSignalAlphaIndex })),
        selectedAlphaIndices: [],
        selectedAlphaIndexSet: new Set<number>(),
      };
    }),
  resultRevision: 0,
  signalCacheOrder: [],
  selectedSignalAlphaIndex: null,
  selectSignalAlpha: (index) => set((state) => {
    if (!state.result || (index === -1 ? !state.result.combined : !state.result.alphas[index])) return state;
    return {
      selectedSignalAlphaIndex: index,
      signalCacheOrder: index >= 0 && state.result.alphas[index].heatmap
        ? [...state.signalCacheOrder.filter(item => item !== index), index] : state.signalCacheOrder,
      ...selectSignalCell(selectSignalSource({ result: state.result, selectedSignalAlphaIndex: index }), state.selectedSymbol, state.selectedTimestamp),
    };
  }),
  cacheAlphaSignals: (index, signals, revision) => set((state) => {
    if (!state.result || state.resultRevision !== revision || index < 0 || !state.result.alphas[index]) return state;
    const alphas = [...state.result.alphas];
    alphas[index] = { ...alphas[index], ...signals };
    const signalCacheOrder = [...state.signalCacheOrder.filter(item => item !== index), index];
    while (signalCacheOrder.length > MAX_CACHED_SIGNAL_ALPHAS) {
      const evicted = signalCacheOrder.shift()!;
      alphas[evicted] = { ...alphas[evicted], heatmap: undefined, signalRange: undefined, distribution: undefined, distributionBins: undefined, distributionRange: undefined, distributionExamples: undefined, signalDistributionLog: undefined };
    }
    return {
      signalCacheOrder,
      result: { ...state.result, alphas },
      ...(state.selectedSignalAlphaIndex === index ? selectSignalCell(signals, state.selectedSymbol, state.selectedTimestamp) : {}),
    };
  }),
  isRunning: false,
  setIsRunning: (running) => set({ isRunning: running }),

  // Selected Cell
  selectedSymbol: null,
  selectedTimestamp: null,
  setSelectedCell: (symbol, timestamp) =>
    set({ selectedSymbol: symbol, selectedTimestamp: timestamp }),

  // View Mode
  viewMode: 'single',
  setViewMode: (mode) => set(state => (state.result?.alphas.length ?? 0) > 1 && mode !== 'detail' ? state : { viewMode: mode }),

  // Detail View
  selectedAlphaIndex: null,
  enterDetailView: (index) =>
    set((state) => {
      if (!state.result || (index === -1 ? !state.result.combined : !state.result.alphas[index])) return state;
      if (state.viewMode === 'detail' && state.selectedAlphaIndex === index) {
        return state;
      }
      return { viewMode: 'detail', selectedAlphaIndex: index, selectedSignalAlphaIndex: index,
        ...selectSignalCell(selectSignalSource({ result: state.result, selectedSignalAlphaIndex: index }), state.selectedSymbol, state.selectedTimestamp) };
    }),
  exitDetailView: () => set((state) => (state.result?.alphas.length ?? 0) > 1 ? state : ({
    viewMode: (state.result?.alphas?.length ?? 0) > 1 ? 'multi' : 'single',
    selectedAlphaIndex: null,
  })),

  // Alpha Selection (multi-alpha save)
  selectedAlphaIndices: [],
  selectedAlphaIndexSet: new Set<number>(),
  toggleAlphaSelection: (index) =>
    set((state) => {
      const exists = state.selectedAlphaIndices.includes(index);
      const selectedAlphaIndices = exists
        ? state.selectedAlphaIndices.filter((i) => i !== index)
        : [...state.selectedAlphaIndices, index];
      return { selectedAlphaIndices, selectedAlphaIndexSet: new Set(selectedAlphaIndices) };
    }),
  toggleAllAlphaSelection: () => set((state) => {
    const indices = state.result?.alphas.flatMap((alpha, index) =>
      !state.gaSession || state.activeResultTab === 'stats' || alpha.round === state.activeResultTab ? [index] : []) ?? [];
    const allSelected = indices.length > 0 && indices.every(index => state.selectedAlphaIndexSet.has(index));
    const visible = new Set(indices);
    const selectedAlphaIndices = allSelected ? state.selectedAlphaIndices.filter(index => !visible.has(index))
      : Array.from(new Set([...state.selectedAlphaIndices, ...indices])).sort((a, b) => a - b);
    return { selectedAlphaIndices, selectedAlphaIndexSet: new Set(selectedAlphaIndices) };
  }),
  clearAlphaSelection: () => set({ selectedAlphaIndices: [], selectedAlphaIndexSet: new Set() }),

  // Modal States
  isSaveModalOpen: false,
  setIsSaveModalOpen: (open) => set({ isSaveModalOpen: open }),
  isLoadModalOpen: false,
  setIsLoadModalOpen: (open) => set({ isLoadModalOpen: open }),

  // Config (replaces separate dataRange + symbols)
  config: null,
  fetchConfig: async () => {
    try {
      const config = await getConfig();
      set((state) => {
        const s = { ...state.settings };
        s.executionPrice = config.executionPrice ?? 'close';
        if (!config.universes?.includes(s.universe)) s.universe = config.defaultUniverse ?? '';
        const minDate = config.simStart;
        const maxDate = config.simEnd;
        s.simEnd = config.supportsSimEnd && state.config ? s.simEnd : maxDate;
        // If current settings are outside data range, compute fresh defaults
        const outOfRange = s.isStart < minDate || s.isStart > maxDate ||
          s.osStart < minDate || s.osStart > maxDate;
        if (outOfRange) {
          resetSplit(s, minDate, s.simEnd || maxDate);
        }
        return { config, settings: enforceSettingsDates(s, minDate, maxDate) };
      });
    } catch {
      // On failure, keep current hardcoded defaults
    }
  },

  // GA Progress
  gaProgress: null,
  setGAProgress: (progress) => set({ gaProgress: progress }),

  gaSession: false,
  gaFinished: false,
  gaRounds: [],
  activeResultTab: 'stats',
  gaCombinedKey: '',
  gaCombinedCache: {},
  beginGA: (settings, sourceXml) => set((state) => ({
    gaSession: true, gaFinished: false, gaRounds: [], activeResultTab: 'stats',
    gaCombinedKey: '', gaCombinedCache: {},
    result: { combined: null, alphas: [], runSettings: settings, sourceXml },
    resultRevision: state.resultRevision + 1,
    selectedAlphaIndex: null, selectedSignalAlphaIndex: null,
    selectedAlphaIndices: [], selectedAlphaIndexSet: new Set<number>(),
    signalCacheOrder: [], selectedSymbol: null, selectedTimestamp: null, viewMode: 'multi',
  })),
  loadSavedResult: (result) => set((state) => {
    const rounds = Array.from(new Set(result.alphas.map(alpha => alpha.round ?? 0))).sort((a, b) => a - b);
    const view = combinedView({ ...state, result, activeResultTab: 'stats', gaCombinedKey: '', gaCombinedCache: {} }, result.alphas, 'stats');
    const index = view.result?.combined ? -1 : result.alphas.length ? 0 : null;
    return {
      ...view,
      gaSession: true, gaFinished: true, gaRounds: rounds.map(round => ({ round, status: 'completed' as const })), activeResultTab: 'stats',
      gaCombinedCache: {}, resultRevision: state.resultRevision + 1,
      selectedAlphaIndex: index, selectedSignalAlphaIndex: index, viewMode: index === null ? 'multi' : 'detail',
      selectedAlphaIndices: [], selectedAlphaIndexSet: new Set<number>(),
      signalCacheOrder: [], selectedSymbol: null, selectedTimestamp: null,
    };
  }),
  startGARound: (round) => set((state) => {
    if (state.gaRounds.some(item => item.round === round)) return state;
    const previous = state.gaRounds.at(-1)?.round;
    const follow = previous === undefined || state.activeResultTab === previous;
    return {
      gaRounds: [...state.gaRounds, { round, status: 'running' }],
      ...(follow ? { ...combinedView(state, undefined, round), activeResultTab: round, selectedAlphaIndex: null, selectedSignalAlphaIndex: null, viewMode: 'multi' as const } : {}),
    };
  }),
  appendGACandidate: (alpha) => set((state) => {
    if (!state.gaSession || !state.result || state.result.alphas.some(item => item.candidateId === alpha.candidateId)) return state;
    const alphas = [...state.result.alphas, alpha];
    const select = state.selectedAlphaIndex === null && (state.activeResultTab === 'stats' || state.activeResultTab === alpha.round);
    return {
      ...combinedView(state, alphas),
      ...(select ? { selectedAlphaIndex: alphas.length - 1, selectedSignalAlphaIndex: alphas.length - 1, viewMode: 'detail' as const } : {}),
    };
  }),
  endGARound: (round) => set((state) => ({ gaRounds: state.gaRounds.map(item => item.round === round ? { ...item, status: 'completed' } : item) })),
  finishGA: () => set((state) => ({
    gaFinished: true,
    gaRounds: state.gaRounds.map(item => item.status === 'running' ? { ...item, status: 'completed' } : item),
    ...(state.activeResultTab === state.gaRounds.at(-1)?.round ? { ...combinedView(state, undefined, 'stats'), activeResultTab: 'stats' as const } : {}),
    ...singleAlphaSignals(state),
  })),
  stopGA: () => set((state) => !state.gaSession || state.gaFinished ? state : {
    gaFinished: true,
    gaRounds: state.gaRounds.map(item => item.status === 'running' ? { ...item, status: 'stopped' } : item),
    ...singleAlphaSignals(state),
  }),
  clearGA: () => set({ gaSession: false, gaFinished: false, gaRounds: [], activeResultTab: 'stats', gaCombinedKey: '', gaCombinedCache: {} }),
  selectResultTab: (tab) => set((state) => {
    if (tab === state.activeResultTab) return state;
    const alphas = state.result?.alphas ?? [];
    const previous = state.selectedAlphaIndex;
    const eligible = alphas.map((alpha, index) => ({ alpha, index })).filter(item => tab === 'stats' || item.alpha.round === tab);
    eligible.sort((a, b) => {
      const left = Number.isFinite(a.alpha.isMetrics.sharpe) ? a.alpha.isMetrics.sharpe : -Infinity;
      const right = Number.isFinite(b.alpha.isMetrics.sharpe) ? b.alpha.isMetrics.sharpe : -Infinity;
      return right === left ? (a.alpha.candidateId ?? 0) - (b.alpha.candidateId ?? 0) : right > left ? 1 : -1;
    });
    const combined = combinedView(state, alphas, tab);
    const index = previous === -1 && combined.result?.combined ? -1
      : previous !== null && eligible.some(item => item.index === previous) ? previous : eligible[0]?.index ?? null;
    return {
      ...combined,
      activeResultTab: tab, selectedAlphaIndex: index, selectedSignalAlphaIndex: index,
      viewMode: index === null ? 'multi' : 'detail',
      ...selectSignalCell(index === -1 ? combined.result?.combined ?? undefined : index === null ? undefined : alphas[index], state.selectedSymbol, state.selectedTimestamp),
    };
  }),
  cacheGACombined: (key, combined, revision) => set(state => {
    if (!state.gaSession || !state.result || state.resultRevision !== revision) return state;
    return {
      gaCombinedCache: { ...state.gaCombinedCache, [key]: combined },
      ...(state.gaCombinedKey === key ? { result: { ...state.result, combined } } : {}),
    };
  }),
  cacheGADetail: (index, detail, revision) => set((state) => {
    if (!state.gaSession || !state.result || state.resultRevision !== revision || !state.result.alphas[index]) return state;
    const alphas = [...state.result.alphas];
    alphas[index] = { ...alphas[index], ...detail };
    return { result: { ...state.result, alphas } };
  }),

  // Snapshots
  pinnedSnapshots: [],
  addSnapshot: (snapshot) =>
    set((state) => {
      if (state.pinnedSnapshots.length >= 8) return state;
      return { pinnedSnapshots: [...state.pinnedSnapshots, snapshot] };
    }),
  removeSnapshot: (id) =>
    set((state) => ({
      pinnedSnapshots: state.pinnedSnapshots.filter((s) => s.id !== id),
    })),
  updateSnapshotName: (id, name) =>
    set((state) => ({
      pinnedSnapshots: state.pinnedSnapshots.map((s) =>
        s.id === id ? { ...s, name } : s
      ),
    })),
}));
