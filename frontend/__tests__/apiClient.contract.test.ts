import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { runSimulate, runGAStream } from '@/lib/api';

function makeStreamResponse(chunks: string[]): Response {
  let idx = 0;
  const encoder = new TextEncoder();

  const reader = {
    async read() {
      if (idx >= chunks.length) {
        return { done: true, value: undefined };
      }
      const value = encoder.encode(chunks[idx]);
      idx += 1;
      return { done: false, value };
    },
  };

  return {
    ok: true,
    body: {
      getReader: () => reader,
    },
  } as unknown as Response;
}

describe('api client contract', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('runSimulate returns parsed payload on success', async () => {
    const payload = { batches: [{ index: 0, summary: { is: {}, os: {} } }], yearly: [], timeseries: {}, isStartIndex: 0, osStartIndex: 100 };

    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: true,
      json: async () => payload,
    } as unknown as Response);

    const result = await runSimulate('<data expr="close" delay="1"/>', '2020-01-01', '2021-01-01', '2024-01-01', undefined, 3);
    expect(result).toEqual(payload);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [, init] = vi.mocked(globalThis.fetch).mock.calls[0] ?? [];
    const body = init && typeof init === 'object' ? (init as RequestInit).body as string : '';
    expect(JSON.parse(body)).not.toHaveProperty('mode');
    expect(JSON.parse(body)).toMatchObject({ collectSignals: true, collectSignalsTopK: 5, threads: 3, });
  });

  it('runSimulate sends the selected universe and omits an empty one', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({ ok: true, json: async () => ({}) } as unknown as Response);
    await runSimulate('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', undefined, 1, { universe: 'TOP500' });
    await runSimulate('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', undefined, 1, { universe: '' });
    const bodies = vi.mocked(globalThis.fetch).mock.calls.map(call => JSON.parse((call[1] as RequestInit).body as string));
    expect(bodies[0].universe).toBe('TOP500');
    expect('universe' in bodies[1]).toBe(false);
  });

  it('serializes the inclusive end and execution price for simulate and GA', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    await runSimulate('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', undefined, 2, { simEnd: '2024-06-30', executionPrice: 'ohlc4' });
    vi.mocked(globalThis.fetch).mockResolvedValue(makeStreamResponse([JSON.stringify({ type: 'search_complete', evaluated: 0, stopReason: 'stop_round', bestXml: '' }) + '\n']));
    await runGAStream('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', 50, 20, 2, () => {}, undefined, '', 'ohlc4', '2024-06-30');
    const bodies = vi.mocked(globalThis.fetch).mock.calls.map(call => JSON.parse((call[1] as RequestInit).body as string));
    for (const body of bodies) expect(body).toMatchObject({ simStart: '2020-01-01', simEnd: '2024-06-30', executionPrice: 'ohlc4', });
    vi.mocked(globalThis.fetch).mockClear();
    vi.mocked(globalThis.fetch).mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    await runSimulate('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', undefined, 2, { executionPrice: 'vwap' });
    vi.mocked(globalThis.fetch).mockResolvedValue(makeStreamResponse([JSON.stringify({ type: 'search_complete', evaluated: 0, stopReason: 'stop_round', bestXml: '' }) + '\n']));
    await runGAStream('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', 50, 20, 2, () => {}, undefined, '', 'vwap');
    for (const call of vi.mocked(globalThis.fetch).mock.calls)
      expect(JSON.parse((call[1] as RequestInit).body as string).executionPrice).toBe('vwap');
  });

  it('runSimulate forwards abort signal', async () => {
    const payload = { batches: [{ index: 0, summary: { is: {}, os: {} } }], yearly: [], timeseries: {}, isStartIndex: 0, osStartIndex: 100 };
    const ctrl = new AbortController();

    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: true,
      json: async () => payload,
    } as unknown as Response);

    await runSimulate('<x/>', '2020-01-01', '2021-01-01', '2024-01-01', ctrl.signal);

    const [, init] = vi.mocked(globalThis.fetch).mock.calls[0] ?? [];
    const requestInit = init as RequestInit;
    expect(requestInit.signal).toBe(ctrl.signal);
  });

  it('runSimulate surfaces detail messages on http error', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ detail: 'validation failed' }),
    } as unknown as Response);

    await expect(runSimulate('<bad/>', '2020-01-01', '2021-01-01', '2024-01-01')).rejects.toThrow(
      'Simulate failed (400): validation failed'
    );
  });

  it('runSimulate surfaces cpp error field on http error', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ error: 'Invalid date format' }),
    } as unknown as Response);

    await expect(runSimulate('<bad/>', '2020-01-01', 'bad-date', '2024-01-01')).rejects.toThrow(
      'Simulate failed (400): Invalid date format'
    );
  });

  it('runGAStream parses split NDJSON events across chunk boundaries', async () => {
    const events: Array<Record<string, unknown>> = [];
    const payloadLines = [
      JSON.stringify({ type: 'generation_end', generation: 1, totalGenerations: 20, bestSharpe: 0.5, bestXml: '<x/>' }) + '\n',
      JSON.stringify({ type: 'complete', bestXml: '<x/>', summary: { is: {}, os: {} }, yearly: [], timeseries: {}, isStartIndex: 0, osStartIndex: 100 }) + '\n',
    ];
    const chunked = [
      payloadLines[0].slice(0, 30),
      payloadLines[0].slice(30) + payloadLines[1],
    ];

    vi.mocked(globalThis.fetch).mockResolvedValue(makeStreamResponse(chunked));

    await runGAStream('<data expr="close" delay="1"/>', '2020-01-01', '2021-01-01', '2024-01-01', 50, 20, 4, (event) => {
      events.push(event as unknown as Record<string, unknown>);
    });

    expect(events).toHaveLength(2);
    expect((events[0] as Record<string, unknown>).type).toBe('generation_end');
    expect((events[1] as Record<string, unknown>).type).toBe('complete');
  });

  it('rejects a truncated GA stream while preserving already delivered events', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(makeStreamResponse([JSON.stringify({ type: 'round_start', round: 1, size: 1 }) + '\n']));
    const events: unknown[] = [];
    await expect(runGAStream('<xml/>', '2020-01-01', '2021-01-01', '2024-01-01', 200, 40, 20, event => events.push(event))).rejects.toThrow('before completion');
    expect(events).toHaveLength(1);
  });

  it('runGAStream throws detail on non-2xx response', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'internal error' }),
    } as unknown as Response);

    await expect(
      runGAStream('<xml/>', '2020-01-01', '2021-01-01', '2024-01-01', 50, 20, 4, () => {
        // no-op
      })
    ).rejects.toThrow('internal error');
  });

  it('runGAStream throws cpp error field on non-2xx response', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: 'isStart must be earlier than osStart' }),
    } as unknown as Response);

    await expect(
      runGAStream('<xml/>', '2020-01-01', '2025-01-01', '2024-01-01', 50, 20, 4, () => {
        // no-op
      })
    ).rejects.toThrow('isStart must be earlier than osStart');
  });
});
