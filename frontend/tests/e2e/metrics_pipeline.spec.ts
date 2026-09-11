import { test, expect } from '@playwright/test';
import fixture from '../../__tests__/fixtures/cpp_multi.json';
import captured from '../../__tests__/fixtures/capture.json';
import type { StarBatchResponse } from '../../types';

// Actual C++ values; omit one upfront payload to exercise the lazy path.
test('real C++ contract renders signals, uses the selected batch and restores charts after detail', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const batchResponse = structuredClone(fixture) as StarBatchResponse;
  const second = batchResponse.batches[1];
  const secondSignals = { signalPlot: second.signalPlot, signalRange: second.signalRange, distribution: second.distribution };
  delete second.signalPlot; delete second.signalRange; delete second.distribution;
  let requests = 0;
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  await page.route('**/api/simulate', async route => {
    requests++;
    const body = route.request().postDataJSON();
    expect(body.collectSignals).toBe(true);
    expect(body.collectSignalsTopK).toBe(5);
    expect(body.mode).toBe(1);
    if (requests === 1) {
      await route.fulfill({ json: batchResponse });
    } else {
      expect(body.xml).toBe(second.xml);
      expect(body.simStart).toBe(captured.settings.simStart);
      expect(body.osStart).toBe(captured.settings.osStart);
      await route.fulfill({ json: { ...secondSignals, batches: [{ ...second, ...secondSignals }], yearly: second.yearly, timeseries: second.timeseries, isStartIndex: second.isStartIndex, osStartIndex: second.osStartIndex } });
    }
  });
  await page.goto('/');
  await page.getByLabel('IS start date').fill(captured.settings.isStart);
  await page.getByLabel('OS start date').fill(captured.settings.osStart);
  await page.getByLabel('Sim start date').fill(captured.settings.simStart);
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect(page.getByText('Alpha Ranking (2)')).toBeVisible();
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
  await expect(page.getByText('Sampled 10/86 bars · 6/6 symbols')).toBeVisible();

  // Canvas pixels retain actual sign, not cross-sectional rank.
  const col = fixture.combined.signalPlot.values[0];
  for (const sign of [1, -1]) {
    const symbol = col.findIndex(value => value !== null && value * sign > 0);
    await expect.poll(async () => page.getByLabel('Signal heatmap').evaluate((node, row) => {
      const canvas = node as HTMLCanvasElement;
      const pixel = canvas.getContext('2d')!.getImageData(Math.floor(canvas.width / 20), Math.floor(canvas.height * (row + 0.5) / 6), 1, 1).data;
      return pixel[1] > pixel[0] ? 1 : pixel[0] > pixel[1] ? -1 : 0;
    }, symbol)).toBe(sign);
  }
  await page.getByLabel('Bar interval').selectOption('D0'); // Next run setting; cached run stays D1.
  await page.getByLabel('Signals', { exact: true }).selectOption('1');
  await expect(page.getByRole('table', { name: 'Signal data' }).locator('tbody tr').first().locator('td').nth(1)).toHaveText(secondSignals.signalPlot!.values[0][0]!.toFixed(4));
  await expect.poll(() => requests).toBe(2);
  await expect(page.getByLabel('Signal distribution').locator('[data-count]')).toHaveCount(secondSignals.distribution!.counts.length);
  await page.getByLabel('Signals', { exact: true }).selectOption('0');
  await page.getByLabel('Signals', { exact: true }).selectOption('1');
  expect(requests).toBe(2);
  await page.getByRole('button', { name: `View alpha_${second.index}`, exact: true }).click();
  await expect(page.getByText('Yearly Table')).toBeVisible();
  await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
  await page.getByTitle('Close detail view').click();
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
  expect(errors).toEqual([]);
});
