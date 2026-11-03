import type { Page, Route } from '@playwright/test';
import type { SimulationResponse } from '../../src/types';

/** A real candidate stream; detail/signal queries still go through the route handler. */
export async function mockRun(
  page: Page,
  handler: (route: Pick<Route, 'request' | 'fulfill'>) => Promise<unknown>,
) {
  await page.route('**/api/simulate', handler);
  await page.route('**/api/ga/stream', (route) =>
    handler({
      request: () => route.request(),
      fulfill: async (options) => {
        const result = options?.json as SimulationResponse;
        if (!result?.context || !Array.isArray(result.alphas))
          throw new Error('Run fixture must use the canonical result contract');
        const candidates = result.alphas.map((alpha) => {
          const { performance: _performance, signals: _signals, ...candidate } = alpha;
          return {
            ...candidate,
            round: candidate.round ?? 1,
            origin: candidate.origin ?? 'random',
          };
        });
        const events = [
          { type: 'search_start', context: result.context },
          { type: 'round_start', round: 1, size: candidates.length },
          ...candidates.map((candidate) => ({ type: 'candidate', candidate })),
          { type: 'round_end', round: 1, bestSharpe: candidates[0]?.summary?.is.sharpe ?? null },
          {
            type: 'search_complete',
            bestXml: candidates[0]?.xml ?? '',
            stopReason: 'exhausted',
            evaluated: candidates.length,
          },
        ];
        await route.fulfill({
          contentType: 'application/x-ndjson',
          body: events.map((event) => JSON.stringify(event)).join('\n') + '\n',
        });
      },
    }),
  );
}
