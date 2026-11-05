import { test, expect } from '@playwright/test';
import captured from '../unit/fixtures/capture.json';
import fixture from '../unit/fixtures/cpp_multi.json';

test('one start button runs search; repeated candidate selection does not refetch', async ({
  page,
}) => {
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  const details: Record<string, unknown>[] = [];
  await page.route('**/api/simulate', (route) => {
    const request = route.request().postDataJSON();
    details.push(request);
    return route.fulfill({
      json: request.combinedOnly
        ? { ...fixture, alphas: [] }
        : {
            ...fixture,
            combined: null,
            alphas: [fixture.alphas.find((a) => a.xml === request.xml) ?? fixture.alphas[0]],
          },
    });
  });
  await page.route('**/api/ga/runs/mock-run/candidates/*/performance', (route) => {
    const id = Number(route.request().url().split('/candidates/')[1].split('/')[0]);
    details.push({ id });
    return route.fulfill({
      json: { ...fixture, combined: null, alphas: [fixture.alphas.find((a) => a.id === id)] },
    });
  });
  await page.addInitScript(
    ({ alphas, context }) => {
      const original = window.fetch,
        encoder = new TextEncoder();
      (window as unknown as { gaStarts: number }).gaStarts = 0;
      window.fetch = (input, init) => {
        if (!String(input).endsWith('/api/ga/stream')) return original(input, init);
        (window as unknown as { gaStarts: number }).gaStarts++;
        const candidate = (index: number) => {
          const { performance: _p, signals: _s, ...alpha } = alphas[index];
          return { type: 'candidate', candidate: { ...alpha, round: 1 } };
        };
        const stream = new ReadableStream({
          start(controller) {
            const send = (event: unknown) =>
              controller.enqueue(encoder.encode(JSON.stringify(event) + '\n'));
            send({ type: 'search_start', runId: 'mock-run', context });
            send({ type: 'round_start', round: 1, size: 2 });
            send(candidate(0));
            setTimeout(() => {
              send(candidate(1));
              send({ type: 'round_end', round: 1, bestSharpe: null });
              send({
                type: 'search_complete',
                bestXml: alphas[0].xml,
                stopReason: 'exhausted',
                evaluated: 2,
              });
              controller.close();
            }, 1600);
          },
        });
        return Promise.resolve(
          new Response(stream, { headers: { 'Content-Type': 'application/x-ndjson' } }),
        );
      };
    },
    { alphas: fixture.alphas, context: fixture.context },
  );
  await page.goto('/');
  const start = page.getByRole('button', { name: 'Run Backtest (Shift+Enter)', exact: true });
  await expect(page.getByRole('button', { name: 'Run GA', exact: true })).toHaveCount(0);
  await start.click();
  const first = page.getByRole('button', {
    name: `View alpha_${fixture.alphas[0].id}`,
    exact: true,
  });
  await expect(first).toBeVisible();
  await first.click();
  await first.click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { gaStarts: number }).gaStarts))
    .toBe(1);
  await expect(
    page.getByRole('button', { name: `View alpha_${fixture.alphas[1].id}`, exact: true }),
  ).toBeVisible();
  await expect(start).toBeEnabled();
  const before = details.length;
  await first.click();
  await first.click();
  await page.waitForTimeout(100);
  expect(details.length).toBeLessThanOrEqual(before + 1);
  await start.click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { gaStarts: number }).gaStarts))
    .toBe(2);
});
