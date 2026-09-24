import { create } from 'zustand';
import type { BacktestRunResponse, BacktestSettings, ViewMode, PinnedSnapshot, GAProgress, StarConfigResponse, SignalPayload } from '@/types';
import { getConfig } from '@/lib/api';
import { selectSignalCell, selectSignalSource } from '@/lib/signalSelection';

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

  // Snapshots
  pinnedSnapshots: PinnedSnapshot[];
  addSnapshot: (snapshot: PinnedSnapshot) => void;
  removeSnapshot: (id: string) => void;
  updateSnapshotName: (id: string, name: string) => void;
}

export const MAX_CACHED_SIGNAL_ALPHAS = 5;

const defaultSettings: BacktestSettings = {
  simStart: '2020-01-01',
  isStart: '2023-01-01',
  osStart: '2024-01-01',
  barInterval: 'D1',
  nJobs: 5,
  gaPopulation: 50,
};

const defaultXml = `<data expr="close" delay="0"/>`;

/** Ensure isStart < osStart, both clamped to [minDate, maxDate] if provided. */
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
  r.isStart = clamp(r.isStart);
  r.osStart = clamp(r.osStart);

  // Enforce simStart <= isStart < osStart
  if (r.simStart > r.isStart) r.simStart = r.isStart;
  if (r.isStart >= r.osStart) {
    r.osStart = clamp(nextDay(r.isStart));
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
  setViewMode: (mode) => set({ viewMode: mode }),

  // Detail View
  selectedAlphaIndex: null,
  enterDetailView: (index) =>
    set((state) => {
      if (state.viewMode === 'detail' && state.selectedAlphaIndex === index) {
        const baseMode = (state.result?.alphas?.length ?? 0) > 1 ? 'multi' : 'single';
        return { viewMode: baseMode, selectedAlphaIndex: null };
      }
      return { viewMode: 'detail', selectedAlphaIndex: index, selectedSignalAlphaIndex: index,
        ...selectSignalCell(selectSignalSource({ result: state.result, selectedSignalAlphaIndex: index }), state.selectedSymbol, state.selectedTimestamp) };
    }),
  exitDetailView: () => set((state) => ({
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
    const indices = state.result?.alphas.map((_, index) => index) ?? [];
    const allSelected = indices.length > 0 && indices.every(index => state.selectedAlphaIndexSet.has(index));
    const selectedAlphaIndices = allSelected ? [] : indices;
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
        let s = { ...state.settings };
        if (config.mode === 0 || config.mode === 1) s.barInterval = config.mode === 0 ? 'D0' : 'D1';
        const minDate = config.simStart;
        const maxDate = config.simEnd;
        // If current settings are outside data range, compute fresh defaults
        const outOfRange = s.isStart < minDate || s.isStart > maxDate ||
          s.osStart < minDate || s.osStart > maxDate;
        if (outOfRange) {
          // Compute a ~70/30 IS/OS split
          const start = new Date(minDate + 'T12:00:00Z');
          const end = new Date(maxDate + 'T12:00:00Z');
          const totalDays = Math.round((end.getTime() - start.getTime()) / 86400000);
          const isDays = Math.max(1, Math.round(totalDays * 0.7));
          const osStartDate = new Date(start);
          osStartDate.setUTCDate(osStartDate.getUTCDate() + isDays);
          s.simStart = minDate;
          s.isStart = minDate;
          s.osStart = osStartDate.toISOString().slice(0, 10);
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
