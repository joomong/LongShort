import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
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

for (const vp of [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
]) {
  for (const years of [14, 27]) {
    test(`detail view keeps a landscape chart above all ${years} years at ${vp.width}x${vp.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(vp);
      await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
      await mockRun(page, (route) => route.fulfill({ json: withYears(fixture, years) }));
      await page.goto('/');
      await page.getByLabel('Run Backtest (Shift+Enter)').click();
      await page.getByRole('button', { name: 'View alpha_0', exact: true }).click();
      const table = page.getByRole('table', { name: 'Yearly performance' });
      await expect(table.locator('tbody tr')).toHaveCount(years);
      await expect(page.locator('[data-testid="performance-chart-panel"] .uplot')).toBeVisible();
      await page.evaluate(
        () =>
          new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
      );
      const panel = page.getByTestId('yearly-table-panel');
      const m = await panel.evaluate((el) => ({ sh: el.scrollHeight, ch: el.clientHeight }));
      expect(m.sh).toBeLessThanOrEqual(m.ch);
      const chart = (await page.getByTestId('performance-chart-panel').boundingBox())!;
      expect(chart.height).toBeGreaterThanOrEqual(140);
      expect(chart.height).toBeLessThanOrEqual(480);
      expect(chart.width / chart.height).toBeGreaterThanOrEqual(1.99);
      const canvas = (await page
        .locator('[data-testid="performance-chart-panel"] .uplot')
        .boundingBox())!;
      expect(canvas.height).toBeLessThanOrEqual(chart.height + 1);
      const pb = (await panel.boundingBox())!;
      // Fill the report's full width; no 900px cap leaving a gutter beside the XML.
      const report = page.getByTestId('performance-report');
      const rb = (await report.boundingBox())!;
      expect(Math.abs(chart.width - rb.width)).toBeLessThanOrEqual(1);
      expect(Math.abs(pb.width - chart.width)).toBeLessThanOrEqual(1);
      expect(Math.abs(pb.y - (chart.y + chart.height))).toBeLessThanOrEqual(1);
      const reportSize = await report.evaluate((el) => ({
        sh: el.scrollHeight,
        ch: el.clientHeight,
      }));
      expect(reportSize.sh).toBeLessThanOrEqual(reportSize.ch + 1); // fractional flex sizing can round the border by one pixel
      const last = (await table.locator('tbody tr').last().boundingBox())!;
      expect(last.y + last.height).toBeLessThanOrEqual(pb.y + pb.height + 1);
      expect(last.y + last.height).toBeLessThanOrEqual(vp.height);
      if (SHOT_DIR)
        await page.screenshot({
          path: `${SHOT_DIR}/after_${vp.width}x${vp.height}_${years}years.png`,
        });
    });
  }
}
