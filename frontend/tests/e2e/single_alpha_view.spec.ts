import type { SimulationResponse } from '../../src/types';
import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import fixture from '../unit/fixtures/cpp_single.json';
import captured from '../unit/fixtures/capture.json';

test('single-alpha Signals and Performance load only their active view and reuse cached results', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  const requests: { output: string }[] = [];
  await mockRun(page, (route) => {
    const request = route.request().postDataJSON();
    if (route.request().url().endsWith('/api/simulate')) requests.push(request);
    const response = structuredClone(fixture) as SimulationResponse;
    response.alphas[0].signals!.logDistribution = {
      ...response.alphas[0].signals!.distribution!,
      scale: 'linear',
    };
    return route.fulfill({ json: response });
  });
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  const views = page.getByRole('tablist', { name: 'Single alpha view' }),
    signals = views.getByRole('tab', { name: 'Signals', exact: true }),
    performance = views.getByRole('tab', { name: 'Performance', exact: true });
  await expect(signals).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.getByLabel('Signal distribution')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Log', exact: true })).toBeEnabled();
  expect(requests.filter((r) => r.output === 'signals')).toHaveLength(1);
  await performance.click();
  await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
  await expect.poll(() => requests.filter((r) => r.output === 'performance').length).toBe(1);
  await expect(page.getByLabel('Signal heatmap')).toBeHidden();
  const row = page.getByRole('button', { name: `View alpha_${fixture.alphas[0].id}`, exact: true });
  await row.click();
  await signals.focus();
  await page.keyboard.press('ArrowRight');
  await expect(performance).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(signals).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  expect(requests.filter((r) => r.output === 'signals')).toHaveLength(1);
  expect(errors).toEqual([]);
});
