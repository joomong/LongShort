import { test, expect } from '@playwright/test';
import captured from '../../__tests__/fixtures/capture.json';
import fixture from '../../__tests__/fixtures/cpp_single.json';

test('a streamed single-alpha GA opens signal analysis and switches to performance and back', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  const simulate: { metricsOnly?: boolean; signalSeriesOnly?: boolean }[] = [];
  // Full alpha runs only; metrics-only detail and the selected symbol's series are separate requests.
  const fullRuns = () => simulate.filter(request => !request.metricsOnly && !request.signalSeriesOnly);
  await page.route('**/api/simulate', route => {
    const request = route.request().postDataJSON();
    simulate.push(request);
    // Like the engine, a metrics-only body carries no signal payload; full runs add log bins.
    const { signalPlot: _plot, signalRange: _range, distribution: _distribution, ...metricsOnly } = fixture;
    return route.fulfill({ json: request.metricsOnly ? metricsOnly : { ...fixture, signalDistributionLog: fixture.distribution } });
  });
  // The real engine streams candidates; the compatible complete event used by chart fixtures never reaches this path.
  await page.addInitScript(({ batch }) => {
    const original = window.fetch;
    const encoder = new TextEncoder();
    window.fetch = (input, init) => {
      if (!String(input).endsWith('/api/ga/stream')) return original(input, init);
      const events = [
        { type: 'round_start', round: 1, size: 1 },
        { type: 'candidate', candidate: { index: 0, round: 1, xml: batch.xml, summary: batch.summary, yearly: batch.yearly } },
        { type: 'round_end', round: 1 },
        { type: 'search_complete', bestXml: batch.xml, stopReason: 'exhausted', evaluated: 1 },
      ];
      const body = events.map(event => JSON.stringify(event) + '\n').join('');
      return Promise.resolve(new Response(encoder.encode(body), { headers: { 'Content-Type': 'application/x-ndjson' } }));
    };
  }, { batch: fixture.batches[0] });
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  const views = page.getByRole('tablist', { name: 'Single alpha view' });
  const signals = views.getByRole('tab', { name: 'Signals', exact: true });
  const performance = views.getByRole('tab', { name: 'Performance', exact: true });
  await expect(signals).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.getByLabel('Signal distribution')).toBeVisible();
  const log = page.getByRole('button', { name: 'Log', exact: true });
  await expect(log).toBeEnabled();
  await expect(page.getByTestId('alpha-detail')).toBeHidden();
  expect(fullRuns()).toHaveLength(1);

  await performance.click();
  await expect(performance).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
  await expect.poll(() => simulate.filter(request => request.metricsOnly).length).toBe(1);
  await expect(page.getByLabel('Signal heatmap')).toBeHidden();
  // Reclicking the selected row still does nothing.
  const row = page.getByRole('button', { name: 'View alpha_0', exact: true });
  await row.click();
  await expect(performance).toHaveAttribute('aria-selected', 'true');

  await signals.focus();
  await page.keyboard.press('ArrowRight');
  await expect(performance).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(signals).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(log).toBeEnabled();
  // Signals are cached for the result, so returning to them does not re-run the alpha.
  expect(fullRuns()).toHaveLength(1);
  expect(errors).toEqual([]);
});
