import { signalFixture } from '../helpers/signalFixture';
import { mockRun } from '../helpers/mockRun';
import { test, expect, type Page } from '@playwright/test';
import captured from '../unit/fixtures/capture.json';
import fixture from '../unit/fixtures/cpp_multi.json';

async function setup(page: Page, sampled = false) {
  const response = structuredClone(fixture);
  const dates = Array.from({ length: 1500 }, (_, i) =>
    new Date(Date.UTC(2022, 0, 1 + i)).toISOString().slice(0, 10),
  );
  const values = dates.map((_, i) =>
    Array.from({ length: 6 }, (_, j) => (i >= 735 && i <= 765 ? null : Math.sin(i / 40 + j))),
  );
  const plot = signalFixture({
    ...response.alphas[0].signals.plot,
    timestamps: dates.map((date) => Date.parse(date)),
    values,
    sampled,
    rowStride: sampled ? 10 : 1,
    originalRows: dates.length,
  });
  const alpha = { ...response.alphas[0], signals: { ...response.alphas[0].signals, plot } };
  const body = { ...response, alphas: [alpha], combined: null };
  const requests: string[] = [];
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  await mockRun(page, (route) => {
    const symbol = route.request().postDataJSON().signalPlotSymbols?.[0];
    if (symbol) {
      requests.push(symbol);
      const index = plot.symbols.indexOf(symbol);
      return route.fulfill({
        json: {
          ...body,
          alphas: [
            {
              ...alpha,
              signals: {
                ...alpha.signals,
                plot: {
                  ...plot,
                  symbols: [symbol],
                  values: values.map((row) => [row[index]]),
                  sampled: false,
                  rowStride: 1,
                },
              },
            },
          ],
        },
      });
    }
    return route.fulfill({ json: body });
  });
  await page.goto('/');
  await page.getByLabel('IS start date').fill(captured.settings.isStart);
  await page.getByLabel('OS start date').fill(captured.settings.osStart);
  await page.getByLabel('Sim start date').fill(captured.settings.simStart);
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.locator('.uplot:visible')).toHaveCount(1);
  return { requests, dates };
}

async function hoverDate(page: Page, ratio: number) {
  const over = page.locator('.uplot:visible .u-over');
  await over.scrollIntoViewIfNeeded();
  const b = (await over.boundingBox())!;
  await page.mouse.move(b.x + b.width * ratio, b.y + b.height * 0.5);
  await frame(page);
  const badge = page.locator('.chart-cursor-date:visible');
  await expect(badge).toBeVisible();
  return (await badge.textContent())!;
}
async function frame(page: Page) {
  await page.evaluate(
    () => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
  );
}

test('signal chart keeps its instance and zoom, pans horizontally, displays missing dates and virtualizes rows', async ({
  page,
}) => {
  const { dates } = await setup(page);
  await hoverDate(page, 0.5);
  await expect(page.locator('.chart-tooltip:visible')).toContainText('N/A');
  const rows = page.getByRole('table', { name: 'Signal data' }).locator('tbody tr');
  expect(await rows.count()).toBeLessThan(100);
  const scroll = page.getByRole('table', { name: 'Signal data' }).locator('..');
  await scroll.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await expect(page.getByRole('table', { name: 'Signal data' })).toContainText(dates.at(-1)!);

  await hoverDate(page, 0.5);
  await page.mouse.wheel(0, -400);
  await frame(page);
  const before = [await hoverDate(page, 0), await hoverDate(page, 0.999)];
  expect(before[0]).not.toBe(dates[0]);
  await page
    .locator('.uplot:visible')
    .evaluate((el) => el.setAttribute('data-instance-audit', 'retained'));
  await page.setViewportSize({ width: 1600, height: 950 });
  await frame(page);
  await expect(page.locator('.uplot:visible')).toHaveAttribute('data-instance-audit', 'retained');
  expect([await hoverDate(page, 0), await hoverDate(page, 0.999)]).toEqual(before);

  const over = page.locator('.uplot:visible .u-over');
  const b = (await over.boundingBox())!;
  const canvas = page.locator('.uplot:visible canvas');
  const image = await canvas.evaluate((el) => (el as HTMLCanvasElement).toDataURL());
  await page.mouse.move(b.x + b.width * 0.5, b.y + b.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width * 0.5, b.y + b.height * 0.7, { steps: 10 });
  await page.mouse.up();
  await frame(page);
  expect(await canvas.evaluate((el) => (el as HTMLCanvasElement).toDataURL())).toBe(image);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width * 2, b.y + b.height * 0.7, { steps: 10 });
  await page.mouse.up();
  await frame(page);
  expect(await hoverDate(page, 0)).toBe(dates[0]);
  await over.dblclick();
  await frame(page);
  expect(await hoverDate(page, 0)).toBe(dates[0]);
  // At 99.9% of a 1,500-day axis the closest sample is not the final day.
  const lastHover = Date.parse(await hoverDate(page, 0.999));
  expect(Math.abs(lastHover - Date.parse(dates.at(-1)!))).toBeLessThanOrEqual(2 * 86400000);
});

test('performance charts remain fixed under wheel and drag while retaining hover dates', async ({
  page,
}) => {
  await setup(page);
  await page.getByRole('button', { name: 'View alpha_0', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
  const before = [await hoverDate(page, 0), await hoverDate(page, 0.999)];
  const b = (await page.locator('.uplot:visible .u-over').boundingBox())!;
  await page.mouse.move(b.x + b.width * 0.5, b.y + b.height * 0.5);
  await page.mouse.wheel(0, -500);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width * 0.8, b.y + b.height * 0.7, { steps: 8 });
  await page.mouse.up();
  await frame(page);
  expect([await hoverDate(page, 0), await hoverDate(page, 0.999)]).toEqual(before);
  await expect(page.getByText('PnL ($M)', { exact: true })).toBeVisible();
  await expect(page.locator('.chart-tooltip:visible')).toContainText('Net: $-1.01M');
  await expect(page.locator('.chart-tooltip:visible')).toContainText('Gross:');
  // Gross is the solid primary curve; net is dashed.
  await expect(page.getByText('━ Gross', { exact: true })).toBeVisible();
  await expect(page.getByText('┄ Net', { exact: true })).toBeVisible();
  await expect(page.getByText('━ IS', { exact: true })).toBeVisible();
  await expect(page.getByText('━ OS', { exact: true })).toBeVisible();
  // Inspect actual rendered strokes: both curves are amber in IS and green in OS.
  await expect
    .poll(() =>
      page.locator('.uplot:visible canvas').evaluate((node) => {
        const canvas = node as HTMLCanvasElement;
        const { width, height } = canvas;
        const pixels = canvas.getContext('2d')!.getImageData(0, 0, width, height).data;
        const counts = { isAmber: 0, isGreen: 0, osAmber: 0, osGreen: 0 };
        for (let y = 20; y < height - 40; y++) {
          for (let x = Math.floor(width * 0.2); x < width * 0.9; x++) {
            const section = x < width * 0.4 ? 'is' : x > width * 0.7 ? 'os' : null;
            if (!section) continue;
            const offset = (y * width + x) * 4;
            const r = pixels[offset],
              g = pixels[offset + 1],
              b = pixels[offset + 2],
              a = pixels[offset + 3];
            if (a < 100) continue;
            if (r > 150 && g > 80 && g < r * 0.9 && b < g * 0.75)
              counts[section === 'is' ? 'isAmber' : 'osAmber']++;
            if (g > 100 && g > r * 1.3 && g > b * 1.3)
              counts[section === 'is' ? 'isGreen' : 'osGreen']++;
          }
        }
        return (
          counts.isAmber > 10 && counts.osGreen > 10 && counts.isGreen === 0 && counts.osAmber === 0
        );
      }),
    )
    .toBe(true);
});

test('wheel zoom respects input magnitude and panel dragging does not recreate the plot', async ({
  page,
}) => {
  await setup(page);
  const over = page.locator('.uplot:visible .u-over');
  const span = async () =>
    Date.parse(await hoverDate(page, 0.999)) - Date.parse(await hoverDate(page, 0));
  await hoverDate(page, 0.5);
  await page.mouse.wheel(0, -1);
  await frame(page);
  const small = await span();
  await over.dblclick();
  await frame(page);
  await hoverDate(page, 0.5);
  await page.mouse.wheel(0, -100);
  await frame(page);
  expect(await span()).toBeLessThan(small);
  const before = await span();
  await page
    .locator('.uplot:visible')
    .evaluate((el) => el.setAttribute('data-instance-audit', 'retained'));
  const separator = (await page.getByRole('separator').boundingBox())!;
  await page.mouse.move(separator.x + 1, separator.y + 100);
  await page.mouse.down();
  await page.mouse.move(separator.x + 100, separator.y + 100, { steps: 12 });
  await page.mouse.up();
  await frame(page);
  await expect(page.locator('.uplot:visible')).toHaveAttribute('data-instance-audit', 'retained');
  expect(Math.abs((await span()) - before)).toBeLessThanOrEqual(86400000);
});

test('full series are cached across A/B/A selections and invalidated by a new run', async ({
  page,
}) => {
  const { requests } = await setup(page, true);
  const choose = async (symbol: string) => {
    await page.getByPlaceholder('Search symbol').fill(symbol);
    await page.locator(`[data-symbol="${symbol}"]`).click();
    await expect(page.getByText('loading full series…')).toHaveCount(0);
  };
  await expect.poll(() => requests.length).toBe(1);
  await choose('BBB');
  await expect.poll(() => requests.length).toBe(2);
  await choose('AAA');
  await frame(page);
  expect(requests).toEqual(['AAA', 'BBB']);
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect.poll(() => requests.length).toBe(3);
});
