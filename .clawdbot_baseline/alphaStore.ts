import { create } from 'zustand';
import type { BacktestResult, BacktestResultItem, BacktestSettings, ViewMode, HeatmapData, ISMetrics, OSMetrics } from '@/types';

interface AlphaState {
  // XML Editor
  xmlCode: string;
  setXmlCode: (code: string) => void;

  // Backtest Settings
  settings: BacktestSettings;
  setSettings: (settings: Partial<BacktestSettings>) => void;

  // Backtest Result (단일)
  result: BacktestResult | null;
  setResult: (result: BacktestResult | null) => void;
  isRunning: boolean;
  setIsRunning: (running: boolean) => void;

  // Backtest Results (다중)
  results: BacktestResultItem[];
  setResults: (results: BacktestResultItem[]) => void;
  addResult: (result: BacktestResultItem) => void;
  selectedResultIds: number[];
  toggleResultSelection: (id: number) => void;
  clearResults: () => void;

  // Selected Cell (for chart)
  selectedSymbol: string | null;
  selectedTimestamp: string | null;
  setSelectedCell: (symbol: string | null, timestamp: string | null) => void;

  // View Mode
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;

  // Modal States
  isSaveModalOpen: boolean;
  setIsSaveModalOpen: (open: boolean) => void;
  isLoadModalOpen: boolean;
  setIsLoadModalOpen: (open: boolean) => void;
}

const defaultSettings: BacktestSettings = {
  osStart: '2023-01-01',
  osEnd: '2023-06-30',
  isStart: '2023-07-01',
  isEnd: '2023-12-31',
  barInterval: 'D0',
};

// Mock data for UI preview
function generateHeatmapData(): { heatmap: HeatmapData; signalRange: { min: number; max: number }; distribution: number[] } {
  const symbols = ['AAPL', 'GOOGL', 'MSFT', 'AMZN', 'META', 'NVDA', 'TSLA', 'AMD', 'INTC', 'NFLX'];
  const timestamps: string[] = [];
  const values: number[][] = [];

  // Generate 60 days of data
  const startDate = new Date('2023-01-01');
  for (let i = 0; i < 60; i++) {
    const date = new Date(startDate);
    date.setDate(startDate.getDate() + i);
    timestamps.push(date.toISOString().split('T')[0]);

    const row: number[] = [];
    for (let j = 0; j < symbols.length; j++) {
      const value = (Math.random() - 0.5) * 2;
      row.push(Math.round(value * 1000) / 1000);
    }
    values.push(row);
  }

  const distribution = Array.from({ length: 20 }, () => Math.floor(Math.random() * 100) + 10);

  return {
    heatmap: { timestamps, symbols, values },
    signalRange: { min: -1, max: 1 },
    distribution,
  };
}

// 다중 결과 Mock 데이터 생성
function generateMockResults(): BacktestResultItem[] {
  const baseData = generateHeatmapData();

  return [
    {
      id: 1,
      isMetrics: { pnl: 12.34, sharpe: 1.85, mdd: -5.23, turnover: 28, ir: 0.92, corr: 0.35 },
      osMetrics: { pnl: 8.12, sharpe: 1.21 },
      ...baseData,
    },
    {
      id: 2,
      isMetrics: { pnl: 5.67, sharpe: 0.72, mdd: -12.5, turnover: 45, ir: 0.38, corr: 0.22 },
      osMetrics: { pnl: 3.21, sharpe: 0.65 },
      ...generateHeatmapData(),
    },
    {
      id: 3,
      isMetrics: { pnl: -2.15, sharpe: 0.31, mdd: -18.7, turnover: 55, ir: 0.12, corr: 0.08 },
      osMetrics: { pnl: -4.32, sharpe: 0.28 },
      ...generateHeatmapData(),
    },
  ];
}

// 단일 결과 Mock 데이터 (기존 호환성)
function generateMockData(): BacktestResult {
  const baseData = generateHeatmapData();
  return {
    ...baseData,
    isMetrics: { pnl: 12.34, sharpe: 1.85, mdd: -5.23, turnover: 28, ir: 0.92, corr: 0.35 },
    osMetrics: { pnl: 8.12, sharpe: 1.21 },
  };
}

const mockResult = generateMockData();
const mockResults = generateMockResults();

const defaultXml = `<data expr="close" delay="1"/>`;

export const useAlphaStore = create<AlphaState>((set) => ({
  // XML Editor
  xmlCode: defaultXml,
  setXmlCode: (code) => set({ xmlCode: code }),

  // Backtest Settings
  settings: defaultSettings,
  setSettings: (newSettings) =>
    set((state) => ({
      settings: { ...state.settings, ...newSettings },
    })),

  // Backtest Result (initialized with mock data for UI preview)
  result: mockResult,
  setResult: (result) => set({ result }),
  isRunning: false,
  setIsRunning: (running) => set({ isRunning: running }),

  // Backtest Results (다중)
  results: mockResults,
  setResults: (results) => set({ results }),
  addResult: (result) => set((state) => ({ results: [...state.results, result] })),
  selectedResultIds: [],
  toggleResultSelection: (id) =>
    set((state) => ({
      selectedResultIds: state.selectedResultIds.includes(id)
        ? state.selectedResultIds.filter((i) => i !== id)
        : [...state.selectedResultIds, id],
    })),
  clearResults: () => set({ results: [], selectedResultIds: [] }),

  // Selected Cell
  selectedSymbol: null,
  selectedTimestamp: null,
  setSelectedCell: (symbol, timestamp) => set({ selectedSymbol: symbol, selectedTimestamp: timestamp }),

  // View Mode
  viewMode: 'single',
  setViewMode: (mode) => set({ viewMode: mode }),

  // Modal States
  isSaveModalOpen: false,
  setIsSaveModalOpen: (open) => set({ isSaveModalOpen: open }),
  isLoadModalOpen: false,
  setIsLoadModalOpen: (open) => set({ isLoadModalOpen: open }),
}));
