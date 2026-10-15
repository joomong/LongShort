import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import captured from '../../__tests__/fixtures/capture.json';
import fixture from '../../__tests__/fixtures/cpp_multi.json';

test('From/To fits 1440 and sends inclusive dates with Execution', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const requests: Record<string, unknown>[] = [];
  await page.route('**/api/config', route => route.fulfill({ json: {
    ...captured.config, simStart: '2020-01-01', simEnd: '2025-12-31', supportsSimEnd: true,
    executionPrice: 'close', executionPrices: ['close', 'ohlc4'], } }));
  await mockRun(page, route => {
    const body = route.request().postDataJSON();
    requests.push(body);
    return route.fulfill({ json: { ...fixture, simStart: body.simStart, simEnd: body.simEnd, executionPrice: body.executionPrice } });
  });
  await page.goto('/');
  const from = page.getByLabel('Sim start date');
  const to = page.getByLabel('Sim end date');
  await expect(to).toHaveValue('2025-12-31');
  await from.fill('2020-02-01');
  await to.fill('2020-02-11');
  await expect(page.getByLabel('IS start date')).toHaveValue('2020-02-01');
  await expect(page.getByLabel('OS start date')).toHaveValue('2020-02-08');
  await page.getByLabel('Execution price').selectOption('ohlc4');
  const a = await from.boundingBox();
  const b = await to.boundingBox();
  expect(a?.y).toBe(b?.y);
  const toolbar = page.locator('.research-toolbar').filter({ has: from });
  expect(await toolbar.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect((await toolbar.boundingBox())!.height).toBeLessThanOrEqual(70);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= 1440)).toBe(true);
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]).toMatchObject({ simStart: '2020-02-01', simEnd: '2020-02-11', isStart: '2020-02-01', osStart: '2020-02-08', executionPrice: 'ohlc4', });
});
