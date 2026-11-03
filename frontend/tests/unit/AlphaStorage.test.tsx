import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SaveAlphaModal from '@/components/SaveAlphaModal';
import LoadAlphaModal from '@/components/LoadAlphaModal';
import { AlphaStorageButtons } from '@/components/RunControls';
import { canShortcutRun, useRunStore } from '@/runStore';
import type { SavedAlphaInfo, SavedAlphaResults } from '@/types';
import { candidateFixture, contextFixture, engineConfigFixture } from './metricFixtures';
const initial = useRunStore.getState(),
  originalFetch = globalThis.fetch;
const editorXml =
  '<!-- note -->\n<data expr="close" delay="1"/>\n <choice><tsmean days="5|10"/></choice>\t\n';
const metadata = {
  simStart: contextFixture.simStart,
  simEnd: contextFixture.simEnd,
  isStart: contextFixture.isStart,
  osStart: contextFixture.osStart,
  universe: contextFixture.universe,
  executionPrice: contextFixture.executionPrice,
  threads: contextFixture.threads,
};
let calls: { url: string; init?: RequestInit }[] = [],
  saveStatus = 200,
  listing: SavedAlphaInfo[] = [],
  saved: SavedAlphaResults;
const json = (body: unknown, status = 200) =>
  ({ ok: status < 300, status, statusText: '', json: async () => body }) as Response;
beforeEach(() => {
  calls = [];
  saveStatus = 200;
  listing = [];
  saved = { context: contextFixture, rows: [] };
  useRunStore.setState(initial, true);
  useRunStore.getState().setConfig(engineConfigFixture);
  useRunStore.getState().setXmlCode(editorXml);
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith('/api/alphas') && init?.method === 'POST')
      return json(
        saveStatus === 200
          ? { name: 'x', level: 'xml', path: '/data/alphas/x' }
          : { error: 'exists' },
        saveStatus,
      );
    if (url.endsWith('/api/alphas')) return json(listing);
    if (url.endsWith('/results')) return json(saved);
    return json({ error: 'unexpected' }, 500);
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = originalFetch;
});
const posted = () => JSON.parse(String(calls.find((c) => c.init?.method === 'POST')!.init!.body));
function finished() {
  useRunStore.getState().beginRun(contextFixture, editorXml);
  useRunStore.getState().receiveSearchEvents([
    { type: 'candidate', candidate: candidateFixture(5, 2) },
    { type: 'candidate', candidate: candidateFixture(1, 1) },
    { type: 'candidate', candidate: candidateFixture(3, 1, 'bad') },
    { type: 'candidate', candidate: candidateFixture(0, 1) },
    { type: 'search_complete', stopReason: 'exhausted', evaluated: 4, bestXml: '<x/>' },
  ]);
}
function openSave() {
  useRunStore.getState().setAlphaName('  desk ');
  useRunStore.getState().setIsSaveModalOpen(true);
  return render(<SaveAlphaModal />);
}
async function clickLoad(name: string) {
  const label = await screen.findByText(name);
  fireEvent.click(
    within(label.closest('.items-center') as HTMLElement).getByRole('button', { name: 'Load' }),
  );
}

describe('saving canonical results', () => {
  it('enables source saving independently of table selection', () => {
    render(<AlphaStorageButtons />);
    expect(screen.getByLabelText('Save alpha')).toBeEnabled();
    act(() => useRunStore.getState().setXmlCode('  '));
    expect(screen.getByLabelText('Save alpha')).toBeDisabled();
  });
  it('saves editor XML byte-for-byte with current toolbar metadata at XML level', async () => {
    useRunStore.getState().setSettings({ ...metadata, executionPrice: 'ohlc4' });
    openSave();
    expect(screen.getByLabelText('XML only')).toBeChecked();
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(useRunStore.getState().isSaveModalOpen).toBe(false));
    expect(posted()).toEqual({
      name: 'desk',
      xml: editorXml,
      level: 'xml',
      metadata: { ...metadata, executionPrice: 'ohlc4' },
    });
  });
  it('saves every successful candidate in stable ID order with immutable run settings', async () => {
    finished();
    useRunStore.getState().toggleAlphaSelection(0);
    useRunStore.getState().setSettings({ universe: 'TOP3000', osStart: '2023-01-01' });
    openSave();
    fireEvent.click(screen.getByLabelText('XML + results + positions'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(useRunStore.getState().isSaveModalOpen).toBe(false));
    expect(posted()).toMatchObject({ level: 'positions', xml: editorXml, metadata });
    expect(posted().rows).toEqual(
      [0, 1, 5].map((id) => ({ id, round: id === 5 ? 2 : 1, xml: candidateFixture(id).xml })),
    );
  });
  it('blocks results when editor differs or a run is active', () => {
    finished();
    useRunStore.getState().setXmlCode('<different/>');
    openSave();
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
    expect(screen.getByTestId('save-level-reason')).toHaveTextContent('Editor XML differs');
    act(() => useRunStore.getState().beginRun(contextFixture, editorXml));
    expect(screen.getByLabelText('XML + results')).toBeDisabled();
  });
  it('keeps conflict errors visible and does not alter the toolbar name', async () => {
    saveStatus = 409;
    openSave();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already exists');
    expect(useRunStore.getState().isSaveModalOpen).toBe(true);
    expect(useRunStore.getState().alphaName).toBe('  desk ');
  });
  it('blocks empty names and shortcut execution while a modal is open', () => {
    expect(canShortcutRun(useRunStore.getState())).toBe(true);
    useRunStore.getState().setIsSaveModalOpen(true);
    render(<SaveAlphaModal />);
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(canShortcutRun(useRunStore.getState())).toBe(false);
    expect(calls).toHaveLength(0);
  });
});

describe('saved result provenance and cancellation', () => {
  it('loads XML/settings and clears unrelated idle results without fetching result files', async () => {
    finished();
    listing = [{ name: 'plain', xml: '<loaded/>', level: 'xml', metadata }];
    useRunStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    await clickLoad('plain');
    await waitFor(() => expect(useRunStore.getState().isLoadModalOpen).toBe(false));
    expect(useRunStore.getState().xmlCode).toBe('<loaded/>');
    expect(useRunStore.getState().settings).toMatchObject(metadata);
    expect(useRunStore.getState().result).toBeNull();
    expect(calls.some((c) => c.url.endsWith('/results'))).toBe(false);
  });
  it('preserves unavailable saved universe instead of relabeling original metrics', async () => {
    listing = [
      { name: 'old', xml: editorXml, level: 'results', metadata: { ...metadata, universe: 'OLD' } },
    ];
    saved = { context: { ...contextFixture, universe: 'OLD' }, rows: [candidateFixture(7)] };
    useRunStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    await clickLoad('old');
    expect(await screen.findByText(/Original results are preserved/)).toBeInTheDocument();
    expect(useRunStore.getState().result?.context.universe).toBe('OLD');
    expect(useRunStore.getState().result?.alphas[0].id).toBe(7);
    expect(useRunStore.getState().settings.universe).toBe('OLD');
  });
  it('aborts a closed Load transaction and ignores its eventual response', async () => {
    listing = [{ name: 'slow', xml: '<loaded/>', level: 'results', metadata }];
    let resolve!: (r: Response) => void;
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input).endsWith('/results')
        ? new Promise((done) => {
            calls.push({ url: String(input), init });
            resolve = done;
          })
        : base(input, init),
    );
    useRunStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    await clickLoad('slow');
    const signal = calls.find((c) => c.url.endsWith('/results'))!.init!.signal;
    fireEvent.click(screen.getAllByRole('button', { name: 'Close' })[0]);
    act(() => useRunStore.getState().setXmlCode('<new edits/>'));
    await act(async () => resolve(json({ context: contextFixture, rows: [candidateFixture(4)] })));
    expect(signal?.aborted).toBe(true);
    expect(useRunStore.getState().xmlCode).toBe('<new edits/>');
    expect(useRunStore.getState().result).toBeNull();
  });
  it('ignores a Load response after a new search starts', async () => {
    listing = [{ name: 'slow', xml: '<loaded/>', level: 'results', metadata }];
    let resolve!: (r: Response) => void;
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input).endsWith('/results')
        ? new Promise((done) => {
            resolve = done;
          })
        : base(input, init),
    );
    useRunStore.getState().setIsLoadModalOpen(true);
    render(<LoadAlphaModal />);
    await clickLoad('slow');
    act(() =>
      useRunStore.getState().beginRun({ ...contextFixture, universe: 'TOP3000' }, '<new search/>'),
    );
    await act(async () => resolve(json({ context: contextFixture, rows: [candidateFixture(4)] })));
    expect(useRunStore.getState().result?.sourceXml).toBe('<new search/>');
    expect(useRunStore.getState().result?.alphas).toEqual([]);
  });
});
