import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const gaEvents = readFileSync(
  path.resolve(process.cwd(), '../tests/fixtures/pipeline/ga_stream_events.ndjson'),
  'utf-8',
);

test.describe('Metrics pipeline (run-stream -> UI render)', () => {
  test('shows deterministic metrics end-to-end after clicking Run', async ({ page }) => {
    await page.route('**/api/data-range', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          minDate: '2024-01-01',
          maxDate: '2024-12-31',
          transactionFeeRate: 0.001,
        }),
      });
    });

    await page.route('**/api/symbols', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(['AAA', 'BBB', 'CCC']),
      });
    });

    await page.route('**/api/ga/run-stream', async (route) => {
      await route.fulfill({
        status: 200,
        headers: {
          'content-type': 'application/x-ndjson',
        },
        body: gaEvents,
      });
    });

    await page.goto('/');

    await page.getByLabel('Run Backtest (Shift+Enter)').click();

    await expect(page.getByText('Alpha Ranking (2)')).toBeVisible();
    await expect(page.getByText('sortedBy=sharpe')).toBeVisible();

    await expect(page.getByText('Combined')).toBeVisible();
    await expect(page.getByText('+947.5%')).toBeVisible();
    await expect(page.getByText('-60.3%')).toBeVisible();

    await expect(page.getByText('alpha_1')).toBeVisible();
    await expect(page.getByText('+1248.0%')).toBeVisible();

    await page.getByText('alpha_1').click();

    await expect(page.getByText('1Y Table (calendar year)')).toBeVisible();
    const yearlyTable = page.locator('table').filter({ hasText: 'PnL (Net)' }).first();
    await expect(yearlyTable).toBeVisible();
    await expect(yearlyTable.getByRole('cell', { name: '+2.2%' }).first()).toBeVisible();
    await expect(yearlyTable.getByRole('cell', { name: '6.00' })).toBeVisible();
    await expect(yearlyTable.getByRole('cell', { name: '+0.08% / $79' })).toBeVisible();
  });
});
