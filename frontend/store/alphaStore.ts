import { create } from 'zustand';
import type { BacktestRunResponse, BacktestSettings, ViewMode, PinnedSnapshot, GAProgress } from '@/types';
import { getDataRange, getSymbols } from '@/lib/api';

interface AlphaState {
  // XML Editor
  xmlCode: string;
  setXmlCode: (code: string) => void;

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
  toggleAlphaSelection: (index: number) => void;
  clearAlphaSelection: () => void;

  // Modal States
  isSaveModalOpen: boolean;
  setIsSaveModalOpen: (open: boolean) => void;
  isLoadModalOpen: boolean;
  setIsLoadModalOpen: (open: boolean) => void;

  // Data Range
  dataRange: { minDate: string; maxDate: string; transactionFeeRate: number } | null;
  fetchDataRange: () => Promise<void>;

  // Symbols
  symbols: string[];
  fetchSymbols: () => Promise<void>;

  // GA Progress
  gaProgress: GAProgress | null;
  setGAProgress: (progress: GAProgress | null) => void;

  // Snapshots
  pinnedSnapshots: PinnedSnapshot[];
  addSnapshot: (snapshot: PinnedSnapshot) => void;
  removeSnapshot: (id: string) => void;
  updateSnapshotName: (id: string, name: string) => void;
}

const defaultSettings: BacktestSettings = {
  isStart: '2023-01-01',
  isEnd: '2023-06-30',
  osStart: '2023-07-01',
  osEnd: '2023-12-31',
  barInterval: 'D1',
  nJobs: 5,
  gaPopulation: 50,
};

const defaultXml = `<data expr="close" delay="1"/>`;

/** Ensure isStart <= isEnd < osStart <= osEnd, clamped to [min, max] if provided. */
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
    const dt = new Date(d + 'T12:00:00Z'); // noon UTC avoids DST/timezone issues
    dt.setUTCDate(dt.getUTCDate() + 1);
    return dt.toISOString().slice(0, 10);
  };

  // Clamp all to data range
  r.isStart = clamp(r.isStart);
  r.isEnd = clamp(r.isEnd);
  r.osStart = clamp(r.osStart);
  r.osEnd = clamp(r.osEnd);

  // Enforce ordering: isStart <= isEnd
  if (r.isStart > r.isEnd) r.isEnd = r.isStart;
  // Enforce isEnd < osStart
  if (r.isEnd >= r.osStart) r.osStart = clamp(nextDay(r.isEnd));
  // Enforce osStart <= osEnd
  if (r.osStart > r.osEnd) r.osEnd = r.osStart;

  // After all enforcement, final safety check.
  // Note: if the data range is only 1 day, isEnd will fall below minDate here.
  // This is acceptable since the backend validator will reject such ranges.
  if (r.isEnd >= r.osStart) {
    // osStart is at maxDate boundary, push isEnd back
    const pd = new Date(r.osStart + 'T12:00:00Z'); // noon UTC avoids DST/timezone issues
    pd.setUTCDate(pd.getUTCDate() - 1);
    r.isEnd = pd.toISOString().slice(0, 10);
    // isStart may need adjustment too
    if (r.isStart > r.isEnd) r.isStart = r.isEnd;
  }

  return r;
}

export const useAlphaStore = create<AlphaState>((set) => ({
  // XML Editor
  xmlCode: defaultXml,
  setXmlCode: (code) => set({ xmlCode: code }),

  // UI Error
  backtestError: null,
  setBacktestError: (message) => set({ backtestError: message }),

  // Backtest Settings
  settings: defaultSettings,
  setSettings: (newSettings) =>
    set((state) => ({
      settings: enforceSettingsDates(
        { ...state.settings, ...newSettings },
        state.dataRange?.minDate,
        state.dataRange?.maxDate,
      ),
    })),

  // Backtest Result
  result: null,
  setResult: (result) =>
    set((state) => {
      const isMulti = !!(result?.combined || (result?.alphas && result.alphas.length > 1));
      const baseMode = isMulti ? 'multi' : 'single';
      // During GA streaming, keep viewMode if already in multi
      const viewMode = state.gaProgress && state.viewMode === 'multi' ? 'multi' : baseMode;
      return {
        result,
        viewMode,
        selectedAlphaIndex: null,
        selectedAlphaIndices: [],
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
        const baseMode = state.result?.combined ? 'multi' : 'single';
        return { viewMode: baseMode, selectedAlphaIndex: null };
      }
      return { viewMode: 'detail', selectedAlphaIndex: index };
    }),
  exitDetailView: () => set((state) => ({
    viewMode: (state.result?.alphas?.length ?? 0) > 1 ? 'multi' : 'single',
    selectedAlphaIndex: null,
  })),

  // Alpha Selection (multi-alpha save)
  selectedAlphaIndices: [],
  toggleAlphaSelection: (index) =>
    set((state) => {
      const exists = state.selectedAlphaIndices.includes(index);
      return {
        selectedAlphaIndices: exists
          ? state.selectedAlphaIndices.filter((i) => i !== index)
          : [...state.selectedAlphaIndices, index],
      };
    }),
  clearAlphaSelection: () => set({ selectedAlphaIndices: [] }),

  // Modal States
  isSaveModalOpen: false,
  setIsSaveModalOpen: (open) => set({ isSaveModalOpen: open }),
  isLoadModalOpen: false,
  setIsLoadModalOpen: (open) => set({ isLoadModalOpen: open }),

  // Data Range
  dataRange: null,
  fetchDataRange: async () => {
    try {
      const range = await getDataRange();
      set((state) => {
        let s = { ...state.settings };
        // If current settings are largely outside data range, compute fresh defaults
        const outOfRange =
          s.isStart < range.minDate || s.isStart > range.maxDate ||
          s.osEnd < range.minDate || s.osEnd > range.maxDate;
        if (outOfRange) {
          // Compute a ~70/30 IS/OS split
          const start = new Date(range.minDate + 'T12:00:00Z');
          const end = new Date(range.maxDate + 'T12:00:00Z');
          const totalDays = Math.round((end.getTime() - start.getTime()) / 86400000);
          const isDays = Math.max(1, Math.round(totalDays * 0.7));
          const isEndDate = new Date(start);
          isEndDate.setUTCDate(isEndDate.getUTCDate() + isDays);
          const osStartDate = new Date(isEndDate);
          osStartDate.setUTCDate(osStartDate.getUTCDate() + 1);
          s.isStart = range.minDate;
          s.isEnd = isEndDate.toISOString().slice(0, 10);
          s.osStart = osStartDate.toISOString().slice(0, 10);
          s.osEnd = range.maxDate;
        } else {
          s.osEnd = range.maxDate;
        }
        return { dataRange: range, settings: enforceSettingsDates(s, range.minDate, range.maxDate) };
      });
    } catch {
      // On failure, keep current hardcoded defaults
    }
  },

  // Symbols
  symbols: [],
  fetchSymbols: async () => {
    try {
      const symbols = await getSymbols();
      set({ symbols });
    } catch {
      // silently ignore – symbols count is non-critical
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
