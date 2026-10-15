import type { Page, Route } from '@playwright/test';
import type { StarBatchResponse } from '../../types';

/** Existing chart fixtures exercise the compatible complete event over the GA transport. */
export async function mockRun(page: Page, handler: (route: Pick<Route, 'request' | 'fulfill'>) => Promise<unknown>) {
  await page.route('**/api/simulate', handler);
  await page.route('**/api/ga/stream', route => handler({
    request: () => route.request(),
    fulfill: async options => {
      const response = options?.json as StarBatchResponse;
      if (!response) throw new Error('GA chart fixtures must provide a JSON simulation response');
      await route.fulfill({ contentType: 'application/x-ndjson', body: JSON.stringify({
        ...response, type: 'complete', bestXml: response.batches[0]?.xml ?? '<data expr="close"/>',
      }) + '\n' });
    },
  }));
}
