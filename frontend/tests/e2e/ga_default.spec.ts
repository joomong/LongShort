import { test, expect } from '@playwright/test';
import captured from '../../__tests__/fixtures/capture.json';
import fixture from '../../__tests__/fixtures/cpp_multi.json';

test('one start button runs GA and repeated clicks retain detail while the first candidate is still alone', async ({ page }) => {
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  const details: Record<string, unknown>[] = [];
  await page.route('**/api/simulate', route => {
    const request = route.request().postDataJSON(); details.push(request);
    return route.fulfill({ json: request.combinedOnly ? { ...fixture, batches: [] }
      : { ...fixture, batches: [fixture.batches.find(batch => batch.xml === request.xml) ?? fixture.batches[0]] } });
  });
  await page.addInitScript(({ batches }) => {
    const original = window.fetch;
    const encoder = new TextEncoder();
    (window as unknown as { gaStarts: number }).gaStarts = 0;
    window.fetch = (input, init) => {
      if (!String(input).endsWith('/api/ga/stream')) return original(input, init);
      (window as unknown as { gaStarts: number }).gaStarts++;
      const candidate = (index: number) => ({ type: 'candidate', candidate: {
        index, round: 1, xml: batches[index].xml, summary: batches[index].summary, yearly: batches[index].yearly,
      } });
      const stream = new ReadableStream({ start(controller) {
        const send = (event: unknown) => controller.enqueue(encoder.encode(JSON.stringify(event) + '\n'));
        send({ type: 'round_start', round: 1, size: 2 }); send(candidate(0));
        setTimeout(() => {
          send(candidate(1)); send({ type: 'round_end', round: 1 });
          send({ type: 'search_complete', bestXml: batches[0].xml, stopReason: 'exhausted', evaluated: 2 });
          controller.close();
        }, 1600);
      } });
      return Promise.resolve(new Response(stream, { headers: { 'Content-Type': 'application/x-ndjson' } }));
    };
  }, { batches: fixture.batches });
  await page.goto('/');
  const start = page.getByRole('button', { name: 'Run Backtest (Shift+Enter)', exact: true });
  await expect(start).toHaveAttribute('title', 'Start GA (Shift+Enter)');
  await expect(page.getByRole('button', { name: 'Run GA', exact: true })).toHaveCount(0);
  await start.click();
  const first = page.getByRole('button', { name: 'View alpha_0', exact: true });
  await expect(first).toBeVisible();
  await first.click(); await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByTestId('alpha-detail')).toBeVisible();
  expect(details).toHaveLength(0);
  await expect(page.getByRole('button', { name: 'View alpha_1', exact: true })).toBeVisible();
  await expect(start).toBeEnabled();
  await expect(page.locator('[data-testid="alpha-detail"] .uplot canvas')).toHaveCount(1);
  const requests = details.length;
  await first.click(); await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByTestId('signal-analysis')).toHaveCount(0);
  expect(details.length).toBe(requests);
  await page.keyboard.press('Shift+Enter');
  await expect.poll(() => page.evaluate(() => (window as unknown as { gaStarts: number }).gaStarts)).toBe(2);
  await expect(start).toBeEnabled();
});
