import { create } from 'zustand';
import type {
  AlphaResult, EngineConfig, PinnedSnapshot, RequestState, RunContext, RunResult,
  RunSettings, SavedAlphaInfo, SavedAlphaResults, SearchEvent, SearchRound,
  SelectedSignalSeries, SignalPayload, ViewMode,
} from '@/types';

type ResultChannel = 'detail' | 'signals' | 'combined' | 'series';
const idle: RequestState = { status: 'idle' };
const MAX_CACHED_SIGNAL_ALPHAS = 5;

interface RunState {
  xmlCode: string;
  alphaName: string;
  settings: RunSettings;
  config: EngineConfig | null;
  configRequest: RequestState;
  configRevision: number;
  backtestError: string | null;
  result: RunResult | null;
  resultRevision: number;
  isRunning: boolean;
  gaSession: boolean;
  gaRunId: string | null;
  gaFinished: boolean;
  gaRounds: SearchRound[];
  searchRound: number | null;
  activeResultTab: 'stats' | number;
  gaCombinedKey: string;
  combinedXmls: string[];
  gaCombinedCache: Record<string, AlphaResult>;
  signalCacheOrder: number[];
  viewMode: ViewMode;
  selectedAlphaIndex: number | null;
  selectedSignalAlphaIndex: number | null;
  selectedSymbol: string | null;
  selectedTimestamp: number | null;
  selectedSeries: SelectedSignalSeries | null;
  detailRequest: RequestState;
  signalRequest: RequestState;
  combinedRequest: RequestState;
  seriesRequest: RequestState;
  retries: Record<ResultChannel, number>;
  selectedAlphaIndices: number[];
  selectedAlphaIndexSet: Set<number>;
  pinnedSnapshots: PinnedSnapshot[];
  isSaveModalOpen: boolean;
  isLoadModalOpen: boolean;
  loadModalRevision: number;
  setXmlCode: (xml: string) => void;
  setAlphaName: (name: string) => void;
  setSettings: (settings: Partial<RunSettings>) => void;
  setBacktestError: (error: string | null) => void;
  setConfig: (config: EngineConfig) => void;
  setConfigRequest: (state: RequestState) => void;
  retryConfig: () => void;
  beginRun: (context: RunContext, sourceXml: string) => void;
  receiveSearchEvents: (events: SearchEvent[]) => void;
  finishRun: (status: 'completed' | 'stopped', error?: string) => void;
  loadSavedAlpha: (alpha: SavedAlphaInfo, saved?: SavedAlphaResults) => void;
  selectResultTab: (tab: 'stats' | number) => void;
  enterDetailView: (index: number) => void;
  exitDetailView: () => void;
  toggleDetailView: (index: number) => void;
  setSelectedCell: (symbol: string | null, timestamp: number | null) => void;
  cachePerformance: (index: number, alpha: AlphaResult, revision: number) => void;
  cacheSignals: (index: number, signals: SignalPayload, revision: number) => void;
  cacheCombined: (key: string, alpha: AlphaResult, revision: number) => void;
  setSelectedSeries: (series: SelectedSignalSeries | null, revision: number) => void;
  setRequestState: (channel: ResultChannel, state: RequestState, revision: number) => void;
  retryResultData: (channel: ResultChannel) => void;
  toggleAlphaSelection: (index: number) => void;
  toggleAllAlphaSelection: () => void;
  clearAlphaSelection: () => void;
  setIsSaveModalOpen: (open: boolean) => void;
  setIsLoadModalOpen: (open: boolean) => void;
  addSnapshot: (snapshot: PinnedSnapshot) => void;
  removeSnapshot: (id: string) => void;
  updateSnapshotName: (id: string, name: string) => void;
}

export function canShortcutRun(state: Pick<RunState, 'isRunning' | 'isSaveModalOpen' | 'isLoadModalOpen' | 'configRequest'>) {
  return state.configRequest.status === 'ready' && !state.isRunning && !state.isSaveModalOpen && !state.isLoadModalOpen;
}

function nextDay(date: string) {
  const value = new Date(date + 'T12:00:00Z');
  value.setUTCDate(value.getUTCDate() + 1);
  return value.toISOString().slice(0, 10);
}
function resetSplit(settings: RunSettings, from: string, to: string) {
  const start = new Date(from + 'T12:00:00Z');
  const days = Math.round((new Date(to + 'T12:00:00Z').getTime() - start.getTime()) / 86400000);
  start.setUTCDate(start.getUTCDate() + Math.max(1, Math.round(days * 0.7)));
  settings.simStart = from;
  settings.isStart = from;
  settings.osStart = start.toISOString().slice(0, 10);
}
function enforceSettingsDates(settings: RunSettings, min?: string, max?: string): RunSettings {
  const result = { ...settings };
  const clamp = (date: string) => min && date < min ? min : max && date > max ? max : date;
  result.simStart = clamp(result.simStart);
  result.simEnd = result.simEnd ? clamp(result.simEnd) : max ?? '';
  if (result.simEnd && result.simStart > result.simEnd) result.simStart = result.simEnd;
  if (result.simEnd && (result.isStart > result.simEnd || result.osStart > result.simEnd) &&
      !(result.simStart === result.simEnd && result.isStart === result.simStart && result.osStart === nextDay(result.simEnd)))
    resetSplit(result, result.simStart, result.simEnd);
  result.isStart = clamp(result.isStart);
  result.osStart = result.simStart === result.simEnd ? nextDay(result.simStart) : clamp(result.osStart);
  if (result.simStart > result.isStart) result.isStart = result.simStart;
  if (result.isStart >= result.osStart) {
    result.osStart = nextDay(result.isStart);
    if (result.simEnd && result.osStart > result.simEnd && result.simStart !== result.simEnd)
      resetSplit(result, result.simStart, result.simEnd);
  }
  return result;
}

export function selectSignalSource(state: Pick<RunState, 'result' | 'selectedSignalAlphaIndex'>): SignalPayload | undefined {
  const index = state.selectedSignalAlphaIndex;
  return index === -1 ? state.result?.combined?.signals
    : state.result?.alphas[index ?? 0]?.signals;
}
function selectSignalCell(source?: SignalPayload, symbol: string | null = null, timestamp: number | null = null) {
  const plot = source?.plot;
  let selectedTimestamp = plot?.timestamps[0] ?? null;
  if (timestamp !== null && plot?.timestamps.length) {
    let low = 0, high = plot.timestamps.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (plot.timestamps[middle] <= timestamp) low = middle + 1; else high = middle;
    }
    selectedTimestamp = plot.timestamps[Math.max(0, low - 1)];
  }
  return {
    selectedSymbol: symbol && plot?.symbols.includes(symbol) ? symbol : plot?.symbols[0] ?? null,
    selectedTimestamp,
  };
}
type CandidateRow = { alpha: AlphaResult; index: number };
const emptyRows: CandidateRow[] = [];
const rowCache = new WeakMap<AlphaResult[], Map<string, CandidateRow[]>>();
const groupCache = new WeakMap<AlphaResult[], Map<string, { key: string; xmls: string[] }>>();

export function visibleCandidateRows(state: Pick<RunState, 'result' | 'gaSession' | 'activeResultTab'>) {
  const alphas = state.result?.alphas;
  if (!alphas) return emptyRows;
  const key = state.gaSession ? String(state.activeResultTab) : 'all';
  const cached = rowCache.get(alphas)?.get(key);
  if (cached) return cached;
  const rows = alphas.map((alpha, index) => ({ alpha, index }))
    .filter(({ alpha }) => !state.gaSession || state.activeResultTab === 'stats' || alpha.round === state.activeResultTab);
  if (state.gaSession) rows.sort((a, b) => {
    const left = a.alpha.summary?.is.sharpe, right = b.alpha.summary?.is.sharpe;
    const l = typeof left === 'number' && Number.isFinite(left) ? left : -Infinity;
    const r = typeof right === 'number' && Number.isFinite(right) ? right : -Infinity;
    return l === r ? a.alpha.id - b.alpha.id : r > l ? 1 : -1;
  });
  const views = rowCache.get(alphas) ?? new Map<string, CandidateRow[]>();
  views.set(key, rows); rowCache.set(alphas, views);
  return rows;
}
export function combinedMembers(alphas: AlphaResult[], tab: 'stats' | number) {
  const cached = groupCache.get(alphas)?.get(String(tab));
  if (cached) return cached;
  const members = alphas.filter(alpha => alpha.status === 'completed' && alpha.xml && (tab === 'stats' || alpha.round === tab))
    .sort((a, b) => a.id - b.id);
  const group = members.length < 2 ? { key: '', xmls: [] as string[] }
    : { key: members.map(alpha => alpha.id).join(','), xmls: members.map(alpha => alpha.xml!) };
  const groups = groupCache.get(alphas) ?? new Map<string, typeof group>();
  groups.set(String(tab), group); groupCache.set(alphas, groups);
  return group;
}
function combinedView(state: RunState): RunState {
  if (!state.result) return state;
  const group = combinedMembers(state.result.alphas, state.activeResultTab);
  return { ...state, gaCombinedKey: group.key, combinedXmls: group.xmls,
    ...(state.gaCombinedKey !== group.key ? { combinedRequest: state.gaCombinedCache[group.key] ? { status: 'ready' as const } : idle } : {}),
    result: { ...state.result, combined: group.key ? state.gaCombinedCache[group.key] ?? null : null } };
}
function finishState(state: RunState, status: 'completed' | 'stopped', error?: string): RunState {
  if (!state.gaSession) return error ? { ...state, backtestError: error } : state;
  if (state.gaFinished) return error ? { ...state, backtestError: error } : state;
  const followLast = status === 'completed' && state.activeResultTab === state.gaRounds.at(-1)?.round;
  let next = combinedView({ ...state, isRunning: false, gaFinished: true, searchRound: null,
    gaRounds: state.gaRounds.map(round => round.status === 'running' ? { ...round, status } : round),
    activeResultTab: followLast ? 'stats' : state.activeResultTab,
    backtestError: error ?? state.backtestError });
  if (next.result?.alphas.length === 1 && next.result.alphas[0].status === 'completed')
    next = { ...next, viewMode: 'single', selectedAlphaIndex: null, selectedSignalAlphaIndex: 0 };
  return next;
}
const defaultSettings: RunSettings = {
  simStart: '2015-01-01', simEnd: '', isStart: '2016-01-01', osStart: '2024-01-01',
  executionPrice: 'close', universe: '', threads: 20, firstSearch: 200, roundSize: 50, stopRound: 40, stopAlphas: 400,
};

export const useRunStore = create<RunState>((set, get) => ({
  xmlCode: '<data expr="close" delay="1"/>', alphaName: '', settings: defaultSettings,
  config: null, configRequest: idle, configRevision: 0, backtestError: null,
  result: null, resultRevision: 0, isRunning: false,
  gaSession: false, gaRunId: null, gaFinished: false, gaRounds: [], searchRound: null, activeResultTab: 'stats',
  gaCombinedKey: '', combinedXmls: [], gaCombinedCache: {}, signalCacheOrder: [],
  viewMode: 'single', selectedAlphaIndex: null, selectedSignalAlphaIndex: null,
  selectedSymbol: null, selectedTimestamp: null, selectedSeries: null,
  detailRequest: idle, signalRequest: idle, combinedRequest: idle, seriesRequest: idle,
  retries: { detail: 0, signals: 0, combined: 0, series: 0 },
  selectedAlphaIndices: [], selectedAlphaIndexSet: new Set(), pinnedSnapshots: [],
  isSaveModalOpen: false, isLoadModalOpen: false, loadModalRevision: 0,
  setXmlCode: xmlCode => set(state => state.xmlCode === xmlCode ? state : { xmlCode }),
  setAlphaName: alphaName => set({ alphaName }),
  setSettings: patch => set(state => ({ settings: enforceSettingsDates({ ...state.settings, ...patch }, state.config?.simStart, state.config?.simEnd) })),
  setBacktestError: backtestError => set({ backtestError }),
  setConfigRequest: configRequest => set({ configRequest }),
  retryConfig: () => set(state => ({ configRevision: state.configRevision + 1, configRequest: idle })),
  setConfig: config => set(state => {
    const settings = { ...state.settings };
    if (!state.config) { settings.executionPrice = config.executionPrice; settings.simEnd = config.simEnd; }
    if (!config.universes.includes(settings.universe)) settings.universe = config.defaultUniverse;
    if (settings.osStart <= config.simStart || settings.isStart > config.simEnd)
      resetSplit(settings, config.simStart, settings.simEnd || config.simEnd);
    return { config, configRequest: { status: 'ready' }, settings: enforceSettingsDates(settings, config.simStart, config.simEnd) };
  }),
  beginRun: (context, sourceXml) => set(state => ({
    result: { context: Object.freeze({ ...context }), alphas: [], combined: null, sourceXml }, resultRevision: state.resultRevision + 1,
    isRunning: true, backtestError: null, gaSession: true, gaRunId: null, gaFinished: false, gaRounds: [], searchRound: null,
    activeResultTab: 'stats', gaCombinedKey: '', combinedXmls: [], gaCombinedCache: {}, signalCacheOrder: [],
    selectedAlphaIndex: null, selectedSignalAlphaIndex: null, selectedSymbol: null, selectedTimestamp: null, selectedSeries: null,
    selectedAlphaIndices: [], selectedAlphaIndexSet: new Set(), viewMode: 'multi',
    detailRequest: idle, signalRequest: idle, combinedRequest: idle, seriesRequest: idle,
  })),
  receiveSearchEvents: events => set(state => {
    if (!state.result || state.gaFinished) return state;
    let next = state;
    for (const event of events) {
      if (event.type === 'search_start') next = { ...next, gaRunId: event.runId, result: { ...next.result!, context: Object.freeze({ ...event.context }) } };
      else if (event.type === 'round_start') {
        if (next.gaRounds.some(round => round.round === event.round)) continue;
        const previous = next.gaRounds.at(-1)?.round;
        const follow = previous === undefined || next.activeResultTab === previous;
        next = { ...next, searchRound: event.round, gaRounds: [...next.gaRounds, { round: event.round, status: 'running' }],
          ...(follow ? { activeResultTab: event.round, selectedAlphaIndex: null, selectedSignalAlphaIndex: null, viewMode: 'multi' as const } : {}) };
      } else if (event.type === 'candidate') {
        if (next.result!.alphas.some(alpha => alpha.id === event.candidate.id)) continue;
        const alphas = [...next.result!.alphas, event.candidate];
        const select = next.selectedAlphaIndex === null && (next.activeResultTab === 'stats' || next.activeResultTab === event.candidate.round);
        next = { ...next, result: { ...next.result!, alphas },
          ...(select ? { selectedAlphaIndex: alphas.length - 1, selectedSignalAlphaIndex: alphas.length - 1, viewMode: 'detail' as const } : {}) };
      } else if (event.type === 'round_end')
        next = { ...next, gaRounds: next.gaRounds.map(round => round.round === event.round ? { ...round, status: 'completed' } : round) };
      else if (event.type === 'search_complete') next = finishState(next, 'completed');
      else if (event.type === 'error') next = finishState(next, 'stopped', event.error);
    }
    return combinedView(next);
  }),
  finishRun: (status, error) => set(state => finishState(state, status, error)),
  loadSavedAlpha: (alpha, saved) => set(state => {
    const base = { ...state, xmlCode: alpha.xml, alphaName: alpha.name,
      settings: enforceSettingsDates({ ...state.settings, ...alpha.metadata }, state.config?.simStart, state.config?.simEnd) };
    if (!saved) return state.isRunning ? base : {
      ...base, result: null, resultRevision: state.resultRevision + 1,
      gaSession: false, gaRunId: null, gaFinished: false, gaRounds: [], searchRound: null, activeResultTab: 'stats',
      gaCombinedCache: {}, gaCombinedKey: '', combinedXmls: [], signalCacheOrder: [],
      viewMode: 'single', selectedAlphaIndex: null, selectedSignalAlphaIndex: null,
      selectedSymbol: null, selectedTimestamp: null, selectedSeries: null,
      selectedAlphaIndices: [], selectedAlphaIndexSet: new Set(),
      detailRequest: idle, signalRequest: idle, combinedRequest: idle, seriesRequest: idle,
    };
    const rounds = Array.from(new Set(saved.rows.map(row => row.round ?? 0))).sort((a, b) => a - b);
    const next = combinedView({ ...base,
      result: { context: Object.freeze({ ...saved.context }), alphas: saved.rows, combined: null, sourceXml: alpha.xml, savedSource: alpha.name },
      resultRevision: state.resultRevision + 1, gaSession: true, gaRunId: null, gaFinished: true, isRunning: false,
      gaRounds: rounds.map(round => ({ round, status: 'completed' })), searchRound: null, activeResultTab: 'stats',
      gaCombinedCache: {}, gaCombinedKey: '', combinedXmls: [], signalCacheOrder: [], backtestError: null,
      selectedAlphaIndex: saved.rows.length ? 0 : null, selectedSignalAlphaIndex: saved.rows.length ? 0 : null,
      selectedSymbol: null, selectedTimestamp: null, selectedSeries: null, selectedAlphaIndices: [], selectedAlphaIndexSet: new Set(),
      viewMode: saved.rows.length ? 'detail' : 'multi', detailRequest: idle, signalRequest: idle, combinedRequest: idle, seriesRequest: idle,
    });
    return next;
  }),
  selectResultTab: tab => set(state => {
    if (tab === state.activeResultTab) return state;
    const next = combinedView({ ...state, activeResultTab: tab });
    const rows = visibleCandidateRows(next), previous = state.selectedAlphaIndex;
    const index = previous === -1 && next.gaCombinedKey ? -1
      : previous !== null && rows.some(row => row.index === previous) ? previous : rows[0]?.index ?? null;
    return { ...next, selectedAlphaIndex: index, selectedSignalAlphaIndex: index,
      viewMode: index === null ? 'multi' : 'detail', selectedSeries: null,
      ...selectSignalCell(index === -1 ? next.result?.combined?.signals : index === null ? undefined : next.result?.alphas[index]?.signals,
        state.selectedSymbol, state.selectedTimestamp), detailRequest: idle, seriesRequest: idle, combinedRequest: idle };
  }),
  enterDetailView: index => set(state => {
    if (!state.result || (index === -1 ? !state.gaCombinedKey && !state.result.combined : !state.result.alphas[index])) return state;
    if (state.viewMode === 'detail' && state.selectedAlphaIndex === index) return state;
    return { viewMode: 'detail', selectedAlphaIndex: index, selectedSignalAlphaIndex: index, selectedSeries: null,
      signalCacheOrder: index >= 0 && state.result.alphas[index].signals?.plot
        ? [...state.signalCacheOrder.filter(value => value !== index), index] : state.signalCacheOrder,
      ...selectSignalCell(index === -1 ? state.result.combined?.signals : state.result.alphas[index].signals, state.selectedSymbol, state.selectedTimestamp),
      detailRequest: idle, seriesRequest: idle };
  }),
  exitDetailView: () => set(state => (state.result?.alphas.length ?? 0) > 1 ? state : { viewMode: 'single', selectedAlphaIndex: null }),
  // Reclicking the row of a finished single-alpha result returns to its signals.
  toggleDetailView: index => {
    const state = get();
    const reclick = state.viewMode === 'detail' && state.selectedAlphaIndex === index;
    const signalsAvailable = (!state.gaSession || state.gaFinished) && !state.result?.alphas[index]?.error;
    if (reclick && signalsAvailable) state.exitDetailView();
    else state.enterDetailView(index);
  },
  setSelectedCell: (selectedSymbol, selectedTimestamp) => set(state => ({ selectedSymbol, selectedTimestamp,
    ...(selectedSymbol !== state.selectedSymbol ? { selectedSeries: null, seriesRequest: idle } : {}) })),
  cachePerformance: (index, alpha, revision) => set(state => {
    if (!state.result || state.resultRevision !== revision || !state.result.alphas[index]) return state;
    const alphas = [...state.result.alphas];
    alphas[index] = { ...alphas[index], summary: alpha.summary,
      yearly: alpha.yearly, performance: alpha.performance, poolCorrelation: alpha.poolCorrelation };
    return { result: { ...state.result, alphas }, detailRequest: { status: 'ready' } };
  }),
  cacheSignals: (index, signals, revision) => set(state => {
    if (!state.result || state.resultRevision !== revision || !state.result.alphas[index]) return state;
    const alphas = [...state.result.alphas]; alphas[index] = { ...alphas[index], signals };
    const signalCacheOrder = [...state.signalCacheOrder.filter(value => value !== index), index];
    while (signalCacheOrder.length > MAX_CACHED_SIGNAL_ALPHAS) {
      const evicted = signalCacheOrder.shift()!; alphas[evicted] = { ...alphas[evicted], signals: undefined };
    }
    return { result: { ...state.result, alphas }, signalCacheOrder, signalRequest: { status: 'ready' },
      ...(state.selectedSignalAlphaIndex === index || state.selectedSignalAlphaIndex === null
        ? selectSignalCell(signals, state.selectedSymbol, state.selectedTimestamp) : {}) };
  }),
  cacheCombined: (key, alpha, revision) => set(state => {
    if (!state.result || state.resultRevision !== revision) return state;
    return { gaCombinedCache: { ...state.gaCombinedCache, [key]: alpha }, combinedRequest: { status: 'ready' },
      ...(state.gaCombinedKey === key ? { result: { ...state.result, combined: alpha } } : {}) };
  }),
  setSelectedSeries: (selectedSeries, revision) => set(state => state.resultRevision !== revision ? state : { selectedSeries, seriesRequest: selectedSeries ? { status: 'ready' } : idle }),
  setRequestState: (channel, value, revision) => set(state => state.resultRevision !== revision ? state : {
    [channel === 'detail' ? 'detailRequest' : channel === 'signals' ? 'signalRequest' : channel === 'combined' ? 'combinedRequest' : 'seriesRequest']: value,
  }),
  retryResultData: channel => set(state => ({ retries: { ...state.retries, [channel]: state.retries[channel] + 1 } })),
  toggleAlphaSelection: index => set(state => {
    const selectedAlphaIndices = state.selectedAlphaIndexSet.has(index)
      ? state.selectedAlphaIndices.filter(value => value !== index) : [...state.selectedAlphaIndices, index];
    return { selectedAlphaIndices, selectedAlphaIndexSet: new Set(selectedAlphaIndices) };
  }),
  toggleAllAlphaSelection: () => set(state => {
    const indices = visibleCandidateRows(state).map(row => row.index), visible = new Set(indices);
    const all = indices.length > 0 && indices.every(index => state.selectedAlphaIndexSet.has(index));
    const selectedAlphaIndices = all ? state.selectedAlphaIndices.filter(index => !visible.has(index))
      : Array.from(new Set([...state.selectedAlphaIndices, ...indices])).sort((a, b) => a - b);
    return { selectedAlphaIndices, selectedAlphaIndexSet: new Set(selectedAlphaIndices) };
  }),
  clearAlphaSelection: () => set({ selectedAlphaIndices: [], selectedAlphaIndexSet: new Set() }),
  setIsSaveModalOpen: isSaveModalOpen => set({ isSaveModalOpen }),
  setIsLoadModalOpen: isLoadModalOpen => set(state => state.isLoadModalOpen === isLoadModalOpen ? state
    : { isLoadModalOpen, loadModalRevision: state.loadModalRevision + 1 }),
  addSnapshot: snapshot => set(state => state.pinnedSnapshots.length >= 8 ? state : { pinnedSnapshots: [...state.pinnedSnapshots, snapshot] }),
  removeSnapshot: id => set(state => ({ pinnedSnapshots: state.pinnedSnapshots.filter(snapshot => snapshot.id !== id) })),
  updateSnapshotName: (id, name) => set(state => ({ pinnedSnapshots: state.pinnedSnapshots.map(snapshot => snapshot.id === id ? { ...snapshot, name } : snapshot) })),
}));
