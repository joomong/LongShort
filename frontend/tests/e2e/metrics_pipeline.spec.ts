import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import multi from '../unit/fixtures/cpp_multi.json';
import single from '../unit/fixtures/cpp_single.json';
import captured from '../unit/fixtures/capture.json';

for (const kind of ['single', 'multi'] as const) {
  test(`${kind} run preserves analysis/detail workflow and original editor`, async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    let requests = 0;
    const fixture = kind === 'single' ? single : multi;
    await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
    await mockRun(page, (route) => {
      requests++;
      return route.fulfill({ json: fixture });
    });
    await page.goto('/');
    await page.getByLabel('IS start date').fill(captured.settings.isStart);
    await page.getByLabel('OS start date').fill(captured.settings.osStart);
    await page.getByLabel('Sim start date').fill(captured.settings.simStart);
    await page.getByLabel('Run Backtest (Shift+Enter)').click();
    await expect(page.getByRole('table', { name: 'Alpha results' })).toBeVisible();
    await expect(page.getByText(/Alpha Ranking/)).toHaveCount(0);
    const rightGap = await page.getByRole('table', { name: 'Alpha results' }).evaluate((table) => {
      const last = table.querySelector('thead tr:last-child th:last-child')!;
      return (
        table.parentElement!.getBoundingClientRect().right - last.getBoundingClientRect().right
      );
    });
    expect(rightGap).toBeGreaterThanOrEqual(23);
    if (kind === 'single') {
      await expect(page.getByLabel('Signal heatmap')).toBeVisible();
      await expect(page.getByLabel('Signal distribution')).toBeVisible();
      await expect(
        page.getByTestId('signal-analysis').getByText('Signals', { exact: true }),
      ).toHaveCount(0);
      await expect(page.getByText('+ Long / − Short', { exact: true })).toHaveCount(0);
      await expect(page.getByText(/Sampled .* bars/)).toHaveCount(0);
    } else {
      await expect(page.getByTestId('signal-analysis')).toHaveCount(0);
      await expect(page.getByTestId('result-placeholder')).toHaveCount(0);
      await page.getByRole('button', { name: 'View combined alpha', exact: true }).click();
      await expect(
        page.getByRole('button', { name: 'View combined alpha', exact: true }),
      ).toHaveAttribute('aria-expanded', 'true');
      await expect(
        page.getByRole('region', { name: 'Selected alpha XML' }).locator('pre'),
      ).toHaveText('No XML available for this alpha.');
      await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
    }
    const editor = page.locator('.monaco-editor').first();
    await expect(editor).toBeVisible({ timeout: 20000 });
    await expect(editor.locator('.view-lines')).toContainText('delay="1"/>');
    const original = await editor.locator('.view-lines').innerText();
    await page.screenshot({ path: `test-results/reference-${kind}-results.png`, fullPage: true });
    for (const batch of fixture.alphas) {
      await page.getByRole('button', { name: `View alpha_${batch.id}`, exact: true }).click();
      await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
      await expect(page.getByTestId('alpha-detail')).toBeVisible();
      await expect(page.getByTitle('Close detail view')).toHaveCount(0);
      await expect(page.getByText('Alpha XML', { exact: true })).toHaveCount(0);
      await expect(page.getByTestId('alpha-detail').getByText(/^#\d+ alpha_/)).toHaveCount(0);
      await expect(
        page
          .getByRole('region', { name: 'Selected alpha XML' })
          .getByText(`alpha_${batch.id}`, { exact: true }),
      ).toHaveCount(0);
      await expect(page.getByLabel('Signal heatmap')).not.toBeVisible();
      for (const name of ['PnL', 'Turnover%', 'Non-zero Pos', 'Max Weight%', 'Drawdown']) {
        await page.getByRole('button', { name, exact: true }).click();
        await expect(page.getByRole('button', { name, exact: true })).toHaveAttribute(
          'aria-pressed',
          'true',
        );
        await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
      }
      await page.getByRole('button', { name: 'PnL', exact: true }).click();
      await expect(page.getByRole('button', { name: 'PnL', exact: true })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      await expect
        .poll(() =>
          page.locator('.uplot:visible canvas').evaluate((el) => (el as HTMLCanvasElement).width),
        )
        .toBeGreaterThan(100);
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
      const plot = await page.locator('.uplot:visible .u-over').boundingBox();
      const chartPanel = (await page.getByTestId('performance-chart-panel').boundingBox())!;
      expect(plot!.width).toBeLessThanOrEqual(chartPanel.width);
      expect(plot!.width).toBeGreaterThan(plot!.height);
      expect(chartPanel.height).toBeLessThanOrEqual(480);
      expect((await page.locator('header').boundingBox())!.height).toBeLessThanOrEqual(76);
      expect(
        await editor.locator('.view-lines').evaluate((el) => getComputedStyle(el).lineHeight),
      ).toBe('16px');
      expect((await editor.locator('.margin').boundingBox())!.width).toBeLessThanOrEqual(45);
      expect(await editor.locator('.view-lines').innerText()).toBe(original);
      await expect(editor).not.toHaveClass(/readonly/);
      await expect(
        page.getByRole('region', { name: 'Selected alpha XML' }).locator('pre'),
      ).toHaveText(batch.xml);
      await page.screenshot({
        path: `test-results/reference-${kind}-detail-${batch.id}.png`,
        fullPage: true,
      });
      await page.getByRole('button', { name: `View alpha_${batch.id}`, exact: true }).click();
      await expect(page.getByTestId('alpha-detail')).toBeVisible();
      if (kind === 'multi') await expect(page.getByTestId('signal-analysis')).toHaveCount(0);
      else await expect(page.getByLabel('Signal heatmap')).not.toBeVisible();
    }
    expect(requests).toBeGreaterThanOrEqual(1);
    expect(errors).toEqual([]);
  });
}
