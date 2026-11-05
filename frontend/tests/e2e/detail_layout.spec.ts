import { mockRun } from '../helpers/mockRun';
import { test, expect, type Page } from '@playwright/test';
import fixture from '../unit/fixtures/cpp_multi.json';
import captured from '../unit/fixtures/capture.json';

const SHOT_DIR = process.env.LAYOUT_SHOT_DIR;

function withYears(body: typeof fixture, count: number) {
  const res = structuredClone(body) as typeof fixture;
  const base = res.alphas[0].yearly[0];
  const years = Array.from({ length: count }, (_, i) => ({
    ...base,
    year: (count === 14 ? 2025 : 2026) - i,
  }));
  res.alphas = res.alphas.map((b) => ({ ...b, yearly: years }));
  if (res.combined) res.combined = { ...res.combined, yearly: years };
  return res;
}

async function openDetail(page: Page, years: number) {
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  await mockRun(page, (route) => route.fulfill({ json: withYears(fixture, years) }));
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await page.getByRole('button', { name: 'View alpha_0', exact: true }).click();
  await expect(
    page.getByRole('table', { name: 'Yearly performance' }).locator('tbody tr'),
  ).toHaveCount(years);
  await expect(page.locator('[data-testid="performance-chart-panel"] .uplot')).toBeVisible();
  await page.evaluate(
    () => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
  );
}

for (const vp of [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
]) {
  test(`detail view chart size ignores the table length and the report scrolls at ${vp.width}x${vp.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(vp);
    const heights: number[] = [];
    for (const years of [14, 27]) {
      await openDetail(page, years);
      const table = page.getByRole('table', { name: 'Yearly performance' });
      const panel = page.getByTestId('yearly-table-panel');
      const report = page.getByTestId('performance-report');
      const chart = (await page.getByTestId('performance-chart-panel').boundingBox())!;
      heights.push(chart.height);
      expect(chart.height).toBeGreaterThanOrEqual(140);
      expect(chart.height).toBeLessThanOrEqual(480);
      const rb = (await report.boundingBox())!;
      // Fill the report's full width; no 900px cap leaving a gutter beside the XML.
      expect(Math.abs(chart.width - rb.width)).toBeLessThanOrEqual(16);
      expect(chart.height).toBeCloseTo(Math.min(chart.width / 2, 480), 0);
      const canvas = (await page
        .locator('[data-testid="performance-chart-panel"] .uplot')
        .boundingBox())!;
      expect(canvas.height).toBeLessThanOrEqual(chart.height + 1);
      const pb = (await panel.boundingBox())!;
      expect(Math.abs(pb.width - chart.width)).toBeLessThanOrEqual(1);
      expect(Math.abs(pb.y - (chart.y + chart.height))).toBeLessThanOrEqual(1);
      const size = await report.evaluate((el) => ({ sh: el.scrollHeight, ch: el.clientHeight }));
      if (chart.height + pb.height > size.ch + 1) expect(size.sh).toBeGreaterThan(size.ch);
      if (years === 27) expect(size.sh).toBeGreaterThan(size.ch);
      await report.evaluate((el) => {
        el.scrollTop = el.scrollHeight;
      });
      const last = (await table.locator('tbody tr').last().boundingBox())!;
      const bottom = last.y + last.height;
      expect(bottom).toBeLessThanOrEqual(rb.y + rb.height + 1);
      expect(bottom).toBeLessThanOrEqual(vp.height);
      expect(last.y).toBeGreaterThanOrEqual(rb.y - 1);
      if (SHOT_DIR)
        await page.screenshot({
          path: `${SHOT_DIR}/after_${vp.width}x${vp.height}_${years}years.png`,
        });
    }
    expect(heights[1]).toBe(heights[0]);
  });
}

test('narrow detail view keeps the 140px minimum chart height', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openDetail(page, 27);
  const panel = page.getByTestId('performance-chart-panel');
  // Narrow the report's own box: a width of 200 would give 100px at 2:1.
  await page.getByTestId('performance-report').evaluate((el) => {
    (el as HTMLElement).style.width = '200px';
  });
  await expect.poll(async () => (await panel.boundingBox())!.width).toBeLessThanOrEqual(200);
  expect((await panel.boundingBox())!.height).toBe(140);
});
