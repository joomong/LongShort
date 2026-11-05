import { beforeEach, describe, expect, it } from 'vitest';
import {
  canShortcutRun,
  combinedMembers,
  selectSignalSource,
  useRunStore,
  visibleCandidateRows,
} from '@/runStore';
import type { SignalPayload } from '@/types';
import {
  candidateFixture,
  contextFixture,
  engineConfigFixture,
  singleAlphaMetricFixture,
} from './metricFixtures';

const initial = useRunStore.getState();
const plot = (value: number): SignalPayload => ({
  plot: {
    timestamps: [1000, 3000],
    symbols: ['A'],
    values: [[value], [value + 1]],
    centers: [value, value + 1],
    scales: [0, 0],
    rowStride: 1,
    sampled: false,
  },
});
beforeEach(() => {
  useRunStore.setState(initial, true);
  useRunStore.getState().setConfig(engineConfigFixture);
});
function receive(...ids: number[]) {
  useRunStore
    .getState()
    .receiveSearchEvents([
      { type: 'round_start', round: 1, size: ids.length },
      ...ids.map((id) => ({ type: 'candidate' as const, candidate: candidateFixture(id) })),
    ]);
}

describe('run state transitions', () => {
  it('begins one immutable run context and resets old result selection', () => {
    const state = useRunStore.getState();
    state.setSelectedCell('old', 1);
    state.beginRun(contextFixture, '<original/>');
    expect(useRunStore.getState()).toMatchObject({
      isRunning: true,
      gaSession: true,
      gaFinished: false,
      selectedSymbol: null,
      selectedTimestamp: null,
    });
    expect(useRunStore.getState().result).toEqual({
      context: contextFixture,
      sourceXml: '<original/>',
      alphas: [],
      combined: null,
    });
    useRunStore.getState().setSettings({ universe: 'TOP3000', osStart: '2023-01-01' });
    expect(useRunStore.getState().result?.context).toEqual(contextFixture);
  });
  it('keeps the run id of the stream it belongs to', () => {
    useRunStore.getState().beginRun(contextFixture, '<x/>');
    useRunStore.getState().receiveSearchEvents([{ type: 'search_start', runId: 'run-1', context: contextFixture }]);
    expect(useRunStore.getState().gaRunId).toBe('run-1');
    useRunStore.getState().beginRun(contextFixture, '<x/>');
    expect(useRunStore.getState().gaRunId).toBeNull();
  });
  it('deduplicates stable IDs and preserves received data on Stop', () => {
    useRunStore.getState().beginRun(contextFixture, '<x/>');
    receive(5, 1, 5);
    expect(useRunStore.getState().result?.alphas.map((a) => a.id)).toEqual([5, 1]);
    useRunStore.getState().finishRun('stopped');
    expect(useRunStore.getState().result?.alphas.map((a) => a.id)).toEqual([5, 1]);
    expect(useRunStore.getState()).toMatchObject({ isRunning: false, gaFinished: true });
    expect(useRunStore.getState().gaRounds).toEqual([{ round: 1, status: 'stopped' }]);
  });
  it('sorts only visible rows, never the underlying candidate identity or combination order', () => {
    useRunStore.getState().beginRun(contextFixture, '<x/>');
    const high = {
      ...candidateFixture(7, 2),
      summary: {
        is: { ...candidateFixture(7).summary!.is, sharpe: 10 },
        os: candidateFixture(7).summary!.os,
      },
    };
    useRunStore.getState().receiveSearchEvents([
      { type: 'candidate', candidate: high },
      { type: 'candidate', candidate: candidateFixture(1, 1) },
      { type: 'candidate', candidate: candidateFixture(3, 1, 'bad') },
    ]);
    const state = useRunStore.getState();
    expect(state.result!.alphas.map((a) => a.id)).toEqual([7, 1, 3]);
    expect(visibleCandidateRows(state).map((r) => r.alpha.id)).toEqual([7, 1, 3]);
    expect(combinedMembers(state.result!.alphas, 'stats').key).toBe('1,7');
    expect(combinedMembers(state.result!.alphas, 1).key).toBe('');
  });
  it('shortcuts require ready config, idle run and no open dialog', () => {
    expect(canShortcutRun(useRunStore.getState())).toBe(true);
    useRunStore.getState().setIsSaveModalOpen(true);
    expect(canShortcutRun(useRunStore.getState())).toBe(false);
    useRunStore.setState({ isSaveModalOpen: false, isLoadModalOpen: true });
    expect(canShortcutRun(useRunStore.getState())).toBe(false);
    useRunStore.setState({
      isLoadModalOpen: false,
      configRequest: { status: 'error', error: 'offline' },
    });
    expect(canShortcutRun(useRunStore.getState())).toBe(false);
  });
});

describe('separate result payloads and bounded caches', () => {
  it('accepts performance without overwriting identity or already loaded signals', () => {
    useRunStore.setState({ result: singleAlphaMetricFixture, selectedSignalAlphaIndex: 0 });
    const revision = useRunStore.getState().resultRevision;
    const signals = plot(9);
    useRunStore.getState().cacheSignals(0, signals, revision);
    useRunStore
      .getState()
      .cachePerformance(
        0,
        {
          ...candidateFixture(77),
          performance: {
            timestamps: [1000],
            cumulativeNet: [0.01],
            cumulativeGross: [0.02],
            drawdown: [0],
            turnover: [0.1],
            maxWeight: [0.2],
            positionCount: [2],
            osStartIndex: 1,
          },
        },
        revision,
      );
    expect(useRunStore.getState().result?.alphas[0].id).toBe(0);
    expect(useRunStore.getState().result?.alphas[0].signals).toBe(signals);
    expect(useRunStore.getState().result?.alphas[0].performance?.cumulativeNet).toEqual([0.01]);
  });
  it('ignores delayed payloads after another run begins', () => {
    useRunStore.setState({ result: singleAlphaMetricFixture });
    const revision = useRunStore.getState().resultRevision;
    useRunStore.getState().beginRun({ ...contextFixture, universe: 'TOP3000' }, '<new/>');
    receive(0);
    useRunStore.getState().cacheSignals(0, plot(-999), revision);
    useRunStore
      .getState()
      .cachePerformance(
        0,
        {
          ...candidateFixture(0),
          performance: {
            timestamps: [1],
            cumulativeNet: [-999],
            cumulativeGross: [0],
            drawdown: [0],
            turnover: [0],
            maxWeight: [0],
            positionCount: [0],
            osStartIndex: 0,
          },
        },
        revision,
      );
    expect(useRunStore.getState().result?.alphas[0].signals).toBeUndefined();
    expect(useRunStore.getState().result?.alphas[0].performance).toBeUndefined();
  });
  it('bounds signal matrices and preserves all computed summaries', () => {
    useRunStore.setState({
      result: {
        ...singleAlphaMetricFixture,
        alphas: Array.from({ length: 7 }, (_, id) => candidateFixture(id)),
      },
    });
    const revision = useRunStore.getState().resultRevision;
    for (let i = 0; i < 5; i++) useRunStore.getState().cacheSignals(i, plot(i), revision);
    useRunStore.getState().enterDetailView(0);
    useRunStore.getState().cacheSignals(5, plot(5), revision);
    const alphas = useRunStore.getState().result!.alphas;
    expect(alphas.filter((a) => a.signals)).toHaveLength(5);
    expect(alphas[0].signals).toBeDefined();
    expect(alphas[1].signals).toBeUndefined();
    expect(alphas[1].summary).toEqual(candidateFixture(1).summary);
  });
  it('uses one selected source and aligns selection to the previous numeric sample', () => {
    const signals = plot(5),
      source = {
        result: { ...singleAlphaMetricFixture, alphas: [{ ...candidateFixture(0), signals }] },
        selectedSignalAlphaIndex: 0,
      };
    expect(selectSignalSource(source)).toBe(signals);
    useRunStore.setState({ ...source, selectedSymbol: 'A', selectedTimestamp: 2000 });
    const revision = useRunStore.getState().resultRevision;
    useRunStore.getState().cacheSignals(0, signals, revision);
    expect(useRunStore.getState()).toMatchObject({ selectedSymbol: 'A', selectedTimestamp: 1000 });
  });
  it('snapshot rename retains original signal arrays', () => {
    const signalSeries = { timestamps: [1000, 2000], values: [1, null] };
    useRunStore.getState().addSnapshot({ id: 's', name: 'one', color: '#ff0000', signalSeries });
    useRunStore.getState().updateSnapshotName('s', 'renamed');
    expect(useRunStore.getState().pinnedSnapshots[0].signalSeries).toBe(signalSeries);
  });
});
