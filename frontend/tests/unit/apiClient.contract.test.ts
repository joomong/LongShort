import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AlphaExistsError,
  getAlphaList,
  getGaCandidatePerformance,
  getSavedResults,
  getSavedRow,
  getSavedBatch,
  requestSimulation,
  saveAlpha,
  streamAlphaSearch,
} from '@/api';
import type { SearchEvent, SearchRequest, SimulationRequest } from '@/types';
import { contextFixture, singleAlphaMetricFixture } from './metricFixtures';

const simulation: SimulationRequest = {
  ...contextFixture,
  xml: '<data expr="close"/>',
  output: 'performance',
};
const search: SearchRequest = {
  ...contextFixture,
  xml: '<data expr="close"/>',
  firstSearch: 200,
  roundSize: 50,
  stopRound: 40,
  stopAlphas: 400,
};
const complete: SearchEvent = {
  type: 'search_complete',
  stopReason: 'exhausted',
  evaluated: 0,
  bestXml: '',
};
function stream(chunks: string[], cancel = vi.fn(async () => {})): Response {
  let i = 0;
  const encoder = new TextEncoder();
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length ? { done: false, value: encoder.encode(chunks[i++]) } : { done: true },
        cancel,
        releaseLock: vi.fn(),
      }),
    },
  } as unknown as Response;
}
const originalFetch = globalThis.fetch;
beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe('canonical API transport', () => {
  it('passes a canonical result through without aliases or data reconstruction', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => singleAlphaMetricFixture,
    } as Response);
    const controller = new AbortController();
    expect(await requestSimulation(simulation, controller.signal)).toBe(singleAlphaMetricFixture);
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toMatch(/\/api\/simulate$/);
    expect(init?.signal).toBe(controller.signal);
    expect(JSON.parse(String(init?.body))).toEqual(simulation);
    expect(JSON.parse(String(init?.body))).not.toHaveProperty('collectSignals');
  });
  it.each(['performance', 'signals', 'signalSeries', 'full'] as const)(
    'preserves explicit %s output scope',
    async (output) => {
      vi.mocked(fetch).mockResolvedValue({
        ok: true,
        json: async () => singleAlphaMetricFixture,
      } as Response);
      await requestSimulation({
        ...simulation,
        output,
        signalPlotSymbols: output === 'signalSeries' ? ['A'] : undefined,
      });
      expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).output).toBe(output);
    },
  );
  it('forwards abort signals on list and every saved result query', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const c = new AbortController();
    await getAlphaList(c.signal);
    await getSavedResults('desk a', c.signal);
    await getSavedRow('desk a', 5, c.signal);
    await getSavedBatch('desk a', c.signal);
    for (const [, init] of vi.mocked(fetch).mock.calls) expect(init?.signal).toBe(c.signal);
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain('desk%20a/results');
  });
  it('reads a stored GA candidate and reports eviction as null', async () => {
    const body = { context: contextFixture, alphas: [], combined: null };
    const c = new AbortController();
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, status: 200, json: async () => body } as Response);
    await expect(getGaCandidatePerformance('run 1', 7, c.signal)).resolves.toEqual(body);
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain('/api/ga/runs/run%201/candidates/7/performance');
    expect(init?.signal).toBe(c.signal);
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ error: 'gone' }) } as Response);
    await expect(getGaCandidatePerformance('run', 7)).resolves.toBeNull();
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({ error: 'bad' }) } as Response);
    await expect(getGaCandidatePerformance('run', 7)).rejects.toThrow('bad');
  });
  it('reports engine errors and preserves conflict distinction', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ error: 'Invalid date' }),
    } as Response);
    await expect(requestSimulation(simulation)).rejects.toThrow('Invalid date');
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: async () => ({ error: 'exists' }),
    } as Response);
    await expect(
      saveAlpha({ name: 'x', xml: '<x/>', level: 'xml', metadata: contextFixture }),
    ).rejects.toBeInstanceOf(AlphaExistsError);
  });
  it('uses status text for a non-JSON transport failure', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new Error('html');
      },
    } as unknown as Response);
    await expect(requestSimulation(simulation)).rejects.toThrow('Bad Gateway');
  });
});

describe('candidate stream transport', () => {
  it('decodes current events across UTF-8/chunk boundaries and sends native search settings', async () => {
    const lines = [{ type: 'round_start', round: 1, size: 1 }, complete]
      .map((v) => JSON.stringify(v) + '\n')
      .join('');
    const cancel = vi.fn(async () => {});
    vi.mocked(fetch).mockResolvedValue(stream([lines.slice(0, 17), lines.slice(17)], cancel));
    const events: SearchEvent[] = [];
    const c = new AbortController();
    await streamAlphaSearch(search, (e) => events.push(e), c.signal);
    expect(events.map((e) => e.type)).toEqual(['round_start', 'search_complete']);
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))).toEqual(search);
    expect(vi.mocked(fetch).mock.calls[0][1]?.signal).toBe(c.signal);
    expect(cancel).toHaveBeenCalledOnce();
  });
  it('rejects a truncated stream without discarding delivered candidates', async () => {
    vi.mocked(fetch).mockResolvedValue(
      stream([JSON.stringify({ type: 'round_start', round: 1, size: 1 }) + '\n']),
    );
    const events: SearchEvent[] = [];
    await expect(streamAlphaSearch(search, (e) => events.push(e))).rejects.toThrow(
      'before completion',
    );
    expect(events).toHaveLength(1);
  });
  it('cancels the reader on malformed JSON or handler error', async () => {
    for (const handlerFails of [false, true]) {
      const cancel = vi.fn(async () => {});
      vi.mocked(fetch).mockResolvedValue(
        stream(
          [
            handlerFails
              ? JSON.stringify({ type: 'round_start', round: 1, size: 1 }) + '\n'
              : '{bad\n',
          ],
          cancel,
        ),
      );
      await expect(
        streamAlphaSearch(search, () => {
          if (handlerFails) throw new Error('handler failed');
        }),
      ).rejects.toThrow();
      expect(cancel).toHaveBeenCalledOnce();
    }
  });
  it('accepts an engine error as terminal and reports HTTP errors before streaming', async () => {
    vi.mocked(fetch).mockResolvedValue(
      stream([JSON.stringify({ type: 'error', error: 'no candidates' })]),
    );
    const events: SearchEvent[] = [];
    await streamAlphaSearch(search, (e) => events.push(e));
    expect(events).toEqual([{ type: 'error', error: 'no candidates' }]);
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ error: 'bad XML' }),
    } as Response);
    await expect(streamAlphaSearch(search, () => {})).rejects.toThrow('bad XML');
  });
});
