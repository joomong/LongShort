import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import ResultsTabs from '@/components/visualization/ResultsTabs';
import AlphaRankingTable from '@/components/multi-alpha/AlphaRankingTable';
import SettingsBar from '@/components/visualization/SettingsBar';
import ControlButtons from '@/components/editor/ControlButtons';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaMetricFixture } from './metricFixtures';

const initial = useAlphaStore.getState();
const settings = { simStart: '2020-01-01', isStart: '2021-01-01', osStart: '2024-01-01', threads: 20 };
const candidate = (id: number, round: number, sharpe: number) => ({
  ...multiAlphaMetricFixture.alphas[0], name: `alpha_${id}`, candidateId: id, round,
  xml: `<data expr="close"/><tsmean days="${id + 1}"/>`,
  isMetrics: { ...multiAlphaMetricFixture.alphas[0].isMetrics, sharpe },
});
beforeEach(() => useAlphaStore.setState(initial, true));

it('keeps the first streamed candidate open when clicked again before its peers arrive', () => {
  const store = useAlphaStore.getState();
  store.beginGA(settings); store.startGARound(1); store.appendGACandidate(candidate(0, 1, 1));
  render(<AlphaRankingTable />);
  const state = useAlphaStore.getState();
  fireEvent.click(screen.getByRole('button', { name: 'View alpha_0' }));
  expect(useAlphaStore.getState()).toBe(state);
  store.enterDetailView(99); store.enterDetailView(-1);
  expect(useAlphaStore.getState()).toBe(state);
});

it('exposes one start button and keeps Stop separate', () => {
  const start = vi.fn(); const stop = vi.fn();
  render(<ControlButtons onRun={start} onStop={stop} />);
  expect(screen.queryByRole('button', { name: 'Run GA' })).not.toBeInTheDocument();
  const button = screen.getByRole('button', { name: 'Run Backtest (Shift+Enter)' });
  expect(button).toHaveAttribute('title', 'Start GA (Shift+Enter)');
  fireEvent.click(button); expect(start).toHaveBeenCalledTimes(1);
  act(() => useAlphaStore.getState().setIsRunning(true));
  expect(button).toBeDisabled(); fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
  expect(stop).toHaveBeenCalledTimes(1);
});

it('starts a round tab immediately and appends all candidates before completion', () => {
  render(<><ResultsTabs /><AlphaRankingTable /></>);
  expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Stats']);
  act(() => { useAlphaStore.getState().beginGA(settings); useAlphaStore.getState().startGARound(1); });
  expect(screen.getByRole('tab', { name: 'Round 1' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.queryByRole('button', { name: 'View alpha_0' })).not.toBeInTheDocument();
  act(() => useAlphaStore.getState().appendGACandidate(candidate(0, 1, 1)));
  expect(screen.getByRole('button', { name: 'View alpha_0' })).toBeInTheDocument();
  act(() => useAlphaStore.getState().appendGACandidate(candidate(1, 1, 3)));
  const rows = within(screen.getByRole('table', { name: 'Alpha results' })).getAllByRole('button', { name: /View alpha_/ });
  expect(rows.map(row => row.getAttribute('aria-label'))).toEqual(['View alpha_1', 'View alpha_0']);
  expect(rows.map(row => row.children[0].textContent)).toEqual(['0', '1']);
  expect(screen.getByRole('button', { name: 'View combined alpha' })).toBeInTheDocument();
  fireEvent.click(rows[0]);
  expect(useAlphaStore.getState().selectedAlphaIndex).toBe(1);
  fireEvent.click(rows[0]);
  expect(useAlphaStore.getState().viewMode).toBe('detail');
  fireEvent.click(rows[1]);
  expect(useAlphaStore.getState().result?.alphas[useAlphaStore.getState().selectedAlphaIndex!].candidateId).toBe(0);
  expect(screen.queryByRole('tab', { name: /random|preview|\+/i })).not.toBeInTheDocument();
});

it('restores the correct aggregate per tab, ignores failed candidates and rejects stale responses', () => {
  const store = useAlphaStore.getState();
  store.beginGA(settings); store.startGARound(1);
  store.appendGACandidate(candidate(8, 1, 2)); store.appendGACandidate(candidate(3, 1, 4));
  const revision = useAlphaStore.getState().resultRevision;
  expect(useAlphaStore.getState().gaCombinedKey).toBe('3,8');
  expect(useAlphaStore.getState().result?.combined?.detailLoaded).toBe(false);
  const combined = { ...candidate(99, 1, 6), name: 'combined', detailLoaded: true };
  store.cacheGACombined('3,8', combined, revision);
  store.enterDetailView(-1); store.endGARound(1); store.startGARound(2);
  store.appendGACandidate(candidate(10, 2, 1));
  store.appendGACandidate({ ...candidate(11, 2, 0), error: 'failed' });
  expect(useAlphaStore.getState().result?.combined).toBeNull();
  store.selectResultTab(1);
  expect(useAlphaStore.getState().result?.combined).toBe(combined);
  store.selectResultTab('stats');
  expect(useAlphaStore.getState().gaCombinedKey).toBe('3,8,10');
  expect(useAlphaStore.getState().result?.combined?.detailLoaded).toBe(false);
  store.beginGA(settings); store.cacheGACombined('3,8', combined, revision);
  expect(useAlphaStore.getState().gaCombinedCache).toEqual({});
});

it('selects only visible round rows while preserving other selections and keeps multi-alpha detail open', () => {
  const store = useAlphaStore.getState();
  store.beginGA(settings); store.startGARound(1);
  store.appendGACandidate(candidate(0, 1, 3)); store.appendGACandidate(candidate(1, 1, 2));
  store.endGARound(1); store.startGARound(2); store.appendGACandidate(candidate(2, 2, 1));
  store.toggleAllAlphaSelection();
  expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([2]);
  store.selectResultTab(1); store.toggleAllAlphaSelection();
  expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([0, 1, 2]);
  store.toggleAllAlphaSelection();
  expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([2]);
  store.enterDetailView(0); store.enterDetailView(0); store.exitDetailView(); store.setViewMode('single');
  expect(useAlphaStore.getState()).toMatchObject({ selectedAlphaIndex: 0, viewMode: 'detail' });
});

it('keeps old rounds available, aggregates unique candidates in Stats and retains stopped results', () => {
  act(() => {
    const store = useAlphaStore.getState(); store.beginGA(settings); store.startGARound(1);
    store.appendGACandidate(candidate(0, 1, 1)); store.endGARound(1); store.startGARound(2);
    store.appendGACandidate(candidate(1, 2, 2));
  });
  render(<><ResultsTabs /><AlphaRankingTable /></>);
  fireEvent.click(screen.getByRole('tab', { name: 'Round 1' }));
  act(() => useAlphaStore.getState().startGARound(3));
  expect(screen.getByRole('tab', { name: 'Round 1' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.queryByRole('button', { name: 'View alpha_1' })).not.toBeInTheDocument();
  act(() => { useAlphaStore.getState().appendGACandidate(candidate(1, 2, 2)); useAlphaStore.getState().stopGA(); });
  fireEvent.click(screen.getByRole('tab', { name: 'Stats' }));
  expect(screen.getAllByRole('button', { name: /View alpha_/ })).toHaveLength(2);
  expect(useAlphaStore.getState().gaRounds.at(-1)?.status).toBe('stopped');
  const revision = useAlphaStore.getState().resultRevision;
  act(() => { useAlphaStore.getState().beginGA(settings); useAlphaStore.getState().cacheGADetail(0, { detailLoaded: true }, revision); });
  expect(useAlphaStore.getState().result?.alphas).toHaveLength(0);
});

it('opens signal analysis when a finished or stopped GA has one alpha and keeps multi-alpha detail', () => {
  const store = useAlphaStore.getState();
  const run = (alphas: ReturnType<typeof candidate>[], end: 'finish' | 'stop' | 'stop between rounds') => {
    store.beginGA(settings); store.startGARound(1);
    for (const alpha of alphas) store.appendGACandidate(alpha);
    expect(useAlphaStore.getState()).toMatchObject({ viewMode: 'detail', selectedAlphaIndex: 0 });
    if (end === 'finish') { store.endGARound(1); store.finishGA(); }
    else if (end === 'stop') store.stopGA();
    else { store.endGARound(1); store.stopGA(); }
    return useAlphaStore.getState();
  };
  expect(run([candidate(0, 1, 1)], 'finish')).toMatchObject({ viewMode: 'single', selectedAlphaIndex: null, selectedSignalAlphaIndex: 0 });
  // The run's trailing stopGA must not undo a view the user chose after completion.
  store.enterDetailView(0); const chosen = useAlphaStore.getState(); store.stopGA();
  expect(useAlphaStore.getState()).toBe(chosen);
  expect(run([candidate(0, 1, 1)], 'stop')).toMatchObject({ viewMode: 'single', selectedAlphaIndex: null });
  expect(run([candidate(0, 1, 1)], 'stop between rounds')).toMatchObject({ viewMode: 'single', selectedAlphaIndex: null, gaFinished: true });
  expect(run([candidate(0, 1, 1), candidate(1, 1, 2)], 'finish')).toMatchObject({ viewMode: 'detail', selectedAlphaIndex: 0 });
  expect(run([{ ...candidate(0, 1, 1), error: 'bad xml' }], 'finish')).toMatchObject({ viewMode: 'detail', selectedAlphaIndex: 0 });
});

it('exposes the four search controls without a fraction counter', () => {
  render(<SettingsBar />);
  expect(screen.getByLabelText('Number of threads')).toHaveValue(20);
  expect(screen.getByLabelText('First search')).toHaveValue(200);
  expect(screen.getByLabelText('Stop round')).toHaveValue(40);
  expect(screen.getByLabelText('Stop alphas')).toHaveValue(400);
  act(() => useAlphaStore.getState().setGAProgress({ generation: 1, totalGenerations: 40, populationSize: 200, evaluated: 0 }));
  expect(screen.getByRole('status')).toHaveTextContent('Round 1');
  expect(screen.getByRole('status')).not.toHaveTextContent('0/200');
});
