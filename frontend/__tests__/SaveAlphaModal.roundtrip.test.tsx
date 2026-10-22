import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SaveAlphaModal from '@/components/modals/SaveAlphaModal';
import LoadAlphaModal from '@/components/modals/LoadAlphaModal';
import { AlphaStorageButtons } from '@/components/editor/ControlButtons';
import AlphaRankingTable from '@/components/multi-alpha/AlphaRankingTable';
import GADetailLoader from '@/components/visualization/GADetailLoader';
import GACombinedLoader from '@/components/visualization/GACombinedLoader';
import { canShortcutRun, useAlphaStore } from '@/store/alphaStore';
import { runCombinedAlphas } from '@/lib/api';
import type { BacktestAlphaResult, SavedAlphaInfo, StarConfigResponse } from '@/types';
import single from './fixtures/cpp_single.json';
import multi from './fixtures/cpp_multi.json';

const initial = useAlphaStore.getState();
const originalFetch = globalThis.fetch;
// Editor XML with choice, alternatives, comment and odd whitespace: must be sent byte-identical.
const editorXml = '<!-- note -->\n<data expr="close" delay="1"/>\n  <choice>\n<tsmean days="5|10"/>\n</choice>\t\n';
const config = { simStart: '2015-01-02', simEnd: '2026-09-15', executionPrice: 'close', executionPrices: ['close', 'ohlc4', 'vwap'],
  universes: ['TOP3000', 'TOP500'], defaultUniverse: 'TOP3000', alphasDir: '/data/alphas', supportsSimEnd: true } as unknown as StarConfigResponse;
const runSettings = { simStart: '2016-01-04', simEnd: '2026-09-15', isStart: '2016-01-04', osStart: '2024-01-01', threads: 20, universe: 'TOP500', executionPrice: 'vwap' as const };
const metric = { pnl: 1, sharpe: 1 };
const row = (id: number, round: number, error?: string): BacktestAlphaResult =>
  ({ name: `alpha_${id}`, rank: id + 1, candidateId: id, round, xml: `<data expr="close" delay="1"/><tsmean days="${id}"/>`, error, isMetrics: metric, osMetrics: metric });

type Call = { url: string; init?: RequestInit };
let calls: Call[] = [];
let saveStatus = 200;
let listing: SavedAlphaInfo[] = [];
let index: unknown = null;
const json = (body: unknown, status = 200) => ({ ok: status < 300, status, statusText: '', json: async () => body }) as Response;

beforeEach(() => {
  calls = []; saveStatus = 200; listing = []; index = null;
  useAlphaStore.setState(initial, true);
  useAlphaStore.setState({ config, xmlCode: editorXml });
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith('/api/alphas') && init?.method === 'POST') return saveStatus === 200 ? json({ name: 'x', saved: true, level: 'xml', path: '/data/alphas/x' }) : json({ error: 'exists' }, saveStatus);
    if (url.endsWith('/api/alphas')) return json(listing);
    if (url.endsWith('/results')) return index ? json(index) : json({ error: 'missing' }, 404);
    if (/\/results\/rows\/\d+$/.test(url)) return json(single);
    if (url.endsWith('/results/batch')) return json(multi);
    return json({ error: 'unexpected' }, 500);
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = originalFetch; });

const posted = () => JSON.parse(String(calls.find(call => call.init?.method === 'POST')!.init!.body));
function finishedResult(sourceXml = editorXml) {
  useAlphaStore.getState().beginGA(runSettings, sourceXml);
  for (const [id, round, error] of [[5, 2], [1, 1], [3, 1, 'boom'], [0, 1]] as const) useAlphaStore.getState().appendGACandidate(row(id, round, error));
  useAlphaStore.getState().finishGA();
}
function openSave() {
  useAlphaStore.getState().setAlphaName('  desk ');
  useAlphaStore.getState().setIsSaveModalOpen(true);
  render(<SaveAlphaModal />);
}

describe('save alpha', () => {
  it('enables the save button from editor XML alone; selection circles do not affect saving', () => {
    const view = render(<AlphaStorageButtons />);
    expect(screen.getByLabelText('Save alpha')).toBeEnabled();
    expect(screen.queryByLabelText('Pin Signal')).toBeNull();
    act(() => useAlphaStore.getState().setXmlCode('  '));
    expect(screen.getByLabelText('Save alpha')).toBeDisabled();
    view.unmount();

    act(() => { useAlphaStore.getState().setXmlCode(editorXml); finishedResult(); });
    render(<AlphaRankingTable />);
    fireEvent.click(screen.getAllByRole('button', { name: 'Select alpha' })[0]);
    expect(useAlphaStore.getState().selectedAlphaIndices).toHaveLength(1);
    expect(useAlphaStore.getState().isSaveModalOpen).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it('saves the editor XML verbatim with toolbar metadata only at the default xml level', async () => {
    useAlphaStore.getState().setSettings({ simStart: '2017-01-03', isStart: '2017-01-03', osStart: '2023-01-02', simEnd: '', universe: '', executionPrice: 'ohlc4' });
    openSave();
    expect(screen.getByText('Directory: /data/alphas')).toBeInTheDocument();
    expect(screen.getByLabelText('XML only')).toBeChecked();
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
    expect(screen.getByLabelText('XML + results + positions')).toBeDisabled();
    expect(screen.getByTestId('save-level-reason')).toHaveTextContent('No finished result');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(useAlphaStore.getState().isSaveModalOpen).toBe(false));
    expect(posted()).toEqual({ name: 'desk', xml: editorXml, level: 'xml',
      metadata: { simStart: '2017-01-03', simEnd: '2026-09-15', isStart: '2017-01-03', osStart: '2023-01-02', universe: 'TOP3000', executionPrice: 'ohlc4' } });
  });

  it('sends successful rows in candidate order with run metadata for results and positions, ignoring selection', async () => {
    finishedResult();
    useAlphaStore.getState().toggleAlphaSelection(0);
    useAlphaStore.getState().setSettings({ osStart: '2022-01-03' }); // toolbar change after the run is not the result's metadata
    openSave();
    fireEvent.click(screen.getByLabelText('XML + results + positions'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(useAlphaStore.getState().isSaveModalOpen).toBe(false));
    const body = posted();
    expect(body).toMatchObject({ name: 'desk', xml: editorXml, level: 'positions',
      metadata: { simStart: '2016-01-04', simEnd: '2026-09-15', isStart: '2016-01-04', osStart: '2024-01-01', universe: 'TOP500', executionPrice: 'vwap' } });
    expect(body.rows).toEqual([0, 1, 5].map(id => ({ id, round: id === 5 ? 2 : 1, xml: row(id, 0).xml })));
  });

  it('disables results levels while running or when the editor XML differs from the run XML', () => {
    finishedResult('<data expr="open" delay="1"/>');
    openSave();
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
    expect(screen.getByTestId('save-level-reason')).toHaveTextContent('Editor XML differs');
    act(() => useAlphaStore.getState().setXmlCode('<data expr="open" delay="1"/>'));
    expect(screen.getByLabelText('XML + results')).toBeEnabled();
    act(() => useAlphaStore.getState().setIsRunning(true));
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
    expect(screen.getByTestId('save-level-reason')).toHaveTextContent('in progress');
  });

  it('reports an existing name (409) and keeps the dialog open', async () => {
    saveStatus = 409;
    openSave();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists');
    expect(screen.getByRole('alert')).toHaveTextContent('Alpha name field');
    expect(useAlphaStore.getState().isSaveModalOpen).toBe(true);
  });

  it('uses the toolbar name: no name input in the dialog, trimmed name sent, store name kept after success', async () => {
    openSave();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByTestId('save-alpha-name')).toHaveTextContent('Name: desk');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(useAlphaStore.getState().isSaveModalOpen).toBe(false));
    expect(posted().name).toBe('desk');
    expect(useAlphaStore.getState().alphaName).toBe('  desk ');
  });

  it('blocks saving with an empty toolbar name and says where to enter it', () => {
    useAlphaStore.getState().setAlphaName('   ');
    useAlphaStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    expect(screen.getByTestId('save-alpha-name')).toHaveTextContent('Alpha name field');
    const save = screen.getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(calls).toHaveLength(0);
  });
});

it('Shift+Enter shortcut is blocked while the save or load dialog is open', () => {
  expect(canShortcutRun(useAlphaStore.getState())).toBe(true);
  useAlphaStore.getState().setIsSaveModalOpen(true);
  expect(canShortcutRun(useAlphaStore.getState())).toBe(false);
  useAlphaStore.setState({ isSaveModalOpen: false, isLoadModalOpen: true });
  expect(canShortcutRun(useAlphaStore.getState())).toBe(false);
});

describe('load alpha', () => {
  const metadata = { simStart: '2018-01-02', simEnd: '2025-12-31', isStart: '2018-06-01', osStart: '2024-01-02', universe: 'TOP500', executionPrice: 'vwap' as const };
  async function load(name: string) {
    useAlphaStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    const heading = await screen.findByText(name);
    fireEvent.click(within(heading.closest('.items-center') as HTMLElement).getByRole('button', { name: 'Load' }));
    await waitFor(() => expect(useAlphaStore.getState().isLoadModalOpen).toBe(false));
  }

  it('restores XML and toolbar settings for an xml-level alpha', async () => {
    listing = [{ name: 'plain', xml: editorXml, level: 'xml', metadata }];
    useAlphaStore.getState().setXmlCode('<data expr="open" delay="1"/>');
    await load('plain');
    expect(useAlphaStore.getState().xmlCode).toBe(editorXml);
    expect(useAlphaStore.getState().settings).toMatchObject(metadata);
    expect(useAlphaStore.getState().result).toBeNull();
    expect(calls.some(call => call.url.includes('/results'))).toBe(false);
  });

  it('falls back to the default universe when the saved one is unavailable and says so', async () => {
    listing = [{ name: 'gone', xml: editorXml, level: 'results', metadata: { ...metadata, universe: 'OLD_V1' } }];
    index = { hasBatch: false, rows: [] };
    useAlphaStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    const heading = await screen.findByText('gone');
    fireEvent.click(within(heading.closest('.items-center') as HTMLElement).getByRole('button', { name: 'Load' }));
    expect(await screen.findByText(/Saved universe "OLD_V1" is unavailable; using "TOP3000"/)).toBeInTheDocument();
    expect(useAlphaStore.getState().xmlCode).toBe(editorXml);
    expect(useAlphaStore.getState().settings.universe).toBe('TOP3000');
    // Replays must use the resolved universe; the engine rejects the unavailable saved one.
    const replay = useAlphaStore.getState().result!.runSettings!;
    expect(replay.universe).toBe('TOP3000');
    await runCombinedAlphas(['<data expr="close" delay="1"/>'], replay).catch(() => undefined); // GACombinedLoader's engine path
    const simulate = calls.find(call => call.url.includes('/api/simulate'))!;
    expect(JSON.parse(String(simulate.init!.body)).universe).toBe('TOP3000');
  });

  it('shows the real error and refuses results if a run started during the fetch', async () => {
    listing = [{ name: 'full', xml: editorXml, level: 'results', metadata }];
    index = { hasBatch: false, rows: [] };
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const base = fetchMock.getMockImplementation() as typeof fetch;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/results')) useAlphaStore.getState().setIsRunning(true);
      return base(input, init);
    });
    useAlphaStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    const heading = await screen.findByText('full');
    fireEvent.click(within(heading.closest('.items-center') as HTMLElement).getByRole('button', { name: 'Load' }));
    expect(await screen.findByText('Stop the running search before loading saved results.')).toBeInTheDocument();
    expect(useAlphaStore.getState().xmlCode).toBe(editorXml); // unchanged from beforeEach; no result attached
    expect(useAlphaStore.getState().result).toBeNull();
  });

  it('shows saved results with detail and Stats batch from saved files, never calling the engine', async () => {
    listing = [{ name: 'full', xml: editorXml, level: 'results', metadata }];
    index = { hasBatch: true, rows: multi.batches.map((batch, i) => ({ ...batch, index: i * 2 + 1, round: i + 1, origin: 'saved', status: 'completed' })) };
    render(<><AlphaRankingTable /><GADetailLoader /><GACombinedLoader /></>);
    await load('full');
    const state = useAlphaStore.getState();
    expect(state.xmlCode).toBe(editorXml);
    expect(state.result).toMatchObject({ savedSource: 'full', sourceXml: editorXml, runSettings: metadata });
    expect(state.gaRounds).toEqual([{ round: 1, status: 'completed' }, { round: 2, status: 'completed' }]);
    expect(screen.getByRole('button', { name: 'View alpha_1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'View alpha_3' })).toBeInTheDocument();
    await waitFor(() => expect(useAlphaStore.getState().result?.combined?.detailLoaded).toBe(true));
    act(() => useAlphaStore.getState().enterDetailView(1));
    await waitFor(() => expect(useAlphaStore.getState().result?.alphas[1].detailLoaded).toBe(true));
    expect(useAlphaStore.getState().result?.alphas[1].isRetSeries).toBeDefined();
    const urls = calls.map(call => call.url);
    expect(urls).toContain('http://localhost:8000/api/alphas/full/results/batch');
    expect(urls).toContain('http://localhost:8000/api/alphas/full/results/rows/3');
    expect(urls.some(url => url.includes('/api/simulate'))).toBe(false);
  });
});
