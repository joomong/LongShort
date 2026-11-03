import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import captured from '../unit/fixtures/capture.json';
import fixture from '../unit/fixtures/cpp_multi.json';

test('Execution selection is sent on each run without an interval mode', async ({ page }) => {
  const requests: Array<{ executionPrice: string }> = [];
  await page.route('**/api/config', (route) =>
    route.fulfill({
      json: {
        ...captured.config,
        executionPrice: 'close',
        executionPrices: ['close', 'ohlc4', 'vwap'],
      },
    }),
  );
  await mockRun(page, (route) => {
    const body = route.request().postDataJSON();
    expect(body).not.toHaveProperty('mode');
    if (route.request().url().endsWith('/api/ga/stream'))
      requests.push({ executionPrice: body.executionPrice });
    return route.fulfill({
      json: { ...fixture, context: { ...fixture.context, executionPrice: body.executionPrice } },
    });
  });
  await page.goto('/');
  const execution = page.getByLabel('Execution price');
  await expect(execution).toBeEnabled();
  await expect(execution).toHaveValue('close');
  await execution.selectOption('ohlc4');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect.poll(() => requests.length).toBe(1);
  await expect(execution).toBeEnabled();
  await execution.selectOption('vwap');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect.poll(() => requests.length).toBe(2);
  await expect(execution).toBeEnabled();
  await execution.selectOption('close');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect.poll(() => requests.length).toBe(3);
  expect(requests).toEqual([
    { executionPrice: 'ohlc4' },
    { executionPrice: 'vwap' },
    { executionPrice: 'close' },
  ]);
  await expect(page.getByLabel('Bar interval')).toHaveCount(0);
});
