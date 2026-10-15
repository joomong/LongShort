import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import fixture from '../../__tests__/fixtures/cpp_single.json';
import captured from '../../__tests__/fixtures/capture.json';

test('gray histogram with a spectrum strip and fixed heatmap keep their geometry across instrument counts', async ({ page }) => {
  let count = 6;
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  await mockRun(page, route => {
    const response = structuredClone(fixture);
    const source = response.signalPlot;
    const plot = { ...source, symbols: Array.from({ length: count }, (_, i) => `S${i}`),
      values: source.timestamps.map((_, t) => Array.from({ length: count }, (_, i) => (i + t) % 7 - 3)),
      sampled: false, originalRows: source.timestamps.length, originalSymbols: count, rowStride: 1 };
    response.signalPlot = plot;
    Object.assign(response.batches[0], { signalPlot: plot });
    return route.fulfill({ json: response });
  });
  await page.goto('/');
  const distribution = page.getByLabel('Signal distribution');
  const panel = page.getByTestId('heatmap-panel');
  const heatmap = page.getByLabel('Signal heatmap');
  for (const instruments of [6, 2000]) {
    count = instruments;
    await page.getByLabel('Run Backtest (Shift+Enter)').click();
    await expect(heatmap).toBeVisible();
    await expect(page.getByText('Distribution', { exact: true })).toHaveCount(0);
    const histogramBounds = (await distribution.boundingBox())!;
    const heatmapBounds = (await panel.boundingBox())!;
    expect(histogramBounds.height).toBe(64);
    expect(heatmapBounds.height).toBe(192);
    expect(heatmapBounds.height / histogramBounds.height).toBe(3);
    expect(await panel.evaluate(el => el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth)).toBe(true);
    const bars = distribution.locator('[data-bin]');
    await expect(bars).toHaveCount(fixture.distribution.counts.length);
    expect(await bars.evaluateAll(nodes => Array.from(new Set(nodes.filter(el => Number(el.getAttribute('data-count')) > 0).map(el => getComputedStyle(el).backgroundColor))))).toEqual(['rgb(115, 115, 115)']);
    await expect(page.getByLabel('Distribution value scale')).toBeVisible();
    expect(await page.getByLabel('Distribution value scale').evaluate(el => getComputedStyle(el).backgroundImage)).toContain('linear-gradient');
    expect(await bars.first().evaluate(el => getComputedStyle(el).marginRight)).toBe('0px');
    await heatmap.hover({ position: { x: heatmapBounds.width * 0.5, y: heatmapBounds.height * 0.5 } });
    const tooltip = page.getByRole('tooltip');
    await expect(tooltip).toBeVisible();
    expect(await tooltip.evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgb(247, 247, 247)');
    expect(await tooltip.locator('div').nth(2).evaluate(el => getComputedStyle(el).color)).toBe('rgb(34, 34, 34)');
  }
});

test('heatmap compares instruments on each date instead of each instrument across time', async ({ page }) => {
  const response = structuredClone(fixture);
  const plot = { ...response.signalPlot,
    symbols: ['A', 'B', 'C', 'D', 'E', 'F'],
    values: response.signalPlot.timestamps.map((_, t) => [10, 20, 30, 40, 50, 60].map(value => value + t * 1000)),
    sampled: false, rowStride: 1,
  };
  response.signalPlot = plot;
  Object.assign(response.batches[0], { signalPlot: plot });
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  await mockRun(page, route => route.fulfill({ json: response }));
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  const heatmap = page.getByLabel('Signal heatmap');
  await expect(heatmap).toBeVisible();
  await expect.poll(() => heatmap.evaluate((node, dates) => {
    const canvas = node as HTMLCanvasElement;
    const ctx = canvas.getContext('2d')!;
    return [1, dates - 2].flatMap(time => [1, 4].map(symbol => {
      const pixel = ctx.getImageData(Math.floor(canvas.width * (time + 0.5) / dates), Math.floor(canvas.height * (symbol + 0.5) / 6), 1, 1).data;
      return Math.sign(pixel[0] - pixel[2]);
    }));
  }, plot.timestamps.length)).toEqual([-1, 1, -1, 1]);
});
