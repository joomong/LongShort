import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { runBacktest, runGAStream } from '@/lib/api';
import type { BacktestSettings } from '@/types';

const settings: BacktestSettings = {
  isStart: '2024-01-02',
  isEnd: '2024-01-04',
  osStart: '2024-01-05',
  osEnd: '2024-01-07',
  barInterval: 'D1',
  nJobs: 2,
  gaPopulation: 10,
};

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

  it('runBacktest returns parsed payload on success', async () => {
    const payload = { combined: null, alphas: [], sortedBy: 'sharpe' };

    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: true,
      json: async () => payload,
    } as unknown as Response);

    const result = await runBacktest('<data expr="close" delay="1"/>', settings);
    expect(result).toEqual(payload);
  });

  it('runBacktest surfaces detail messages on http error', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      json: async () => ({ detail: 'validation failed' }),
    } as unknown as Response);

    await expect(runBacktest('<bad/>', settings)).rejects.toThrow(
      'Backtest failed (400): validation failed'
    );
  });

  it('runGAStream parses split NDJSON events across chunk boundaries', async () => {
    const events: Array<Record<string, unknown>> = [];
    const payloadLines = [
      JSON.stringify({ type: 'progress', generation: 1 }) + '\n',
      JSON.stringify({ type: 'generation_end', generation: 1, partialResult: { alphas: [] } }) + '\n',
      JSON.stringify({ type: 'complete', data: { combined: null, alphas: [] } }) + '\n',
    ];
    const chunked = [
      payloadLines[0] + payloadLines[1].slice(0, 14),
      payloadLines[1].slice(14) + payloadLines[2],
    ];

    vi.mocked(globalThis.fetch).mockResolvedValue(makeStreamResponse(chunked));

    await runGAStream('<data expr="close" delay="1"/>', settings, 10, (event) => {
      events.push(event);
    });

    expect(events).toEqual([
      { type: 'progress', generation: 1 },
      { type: 'generation_end', generation: 1, partialResult: { alphas: [] } },
      { type: 'complete', data: { combined: null, alphas: [] } },
    ]);
  });

  it('runGAStream throws detail on non-2xx response', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'internal error' }),
    } as unknown as Response);

    await expect(
      runGAStream('<xml/>', settings, 10, () => {
        // no-op
      })
    ).rejects.toThrow('internal error');
  });
});
