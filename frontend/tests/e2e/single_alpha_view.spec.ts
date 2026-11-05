import type { SimulationResponse } from '../../src/types';
import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import fixture from '../unit/fixtures/cpp_single.json';
import captured from '../unit/fixtures/capture.json';

test('single-alpha row toggles Signals and Performance, which load only their active view and reuse cached results', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  const requests: { output: string }[] = [];
  const { storedReads } = await mockRun(page, (route) => {
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
  const row = page.getByRole('button', { name: `View alpha_${fixture.alphas[0].id}`, exact: true });
  await expect(page.getByRole('tablist', { name: 'Single alpha view' })).toHaveCount(0);
  await expect(row).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await expect(page.getByLabel('Signal distribution')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Log', exact: true })).toBeEnabled();
  expect(requests.filter((r) => r.output === 'signals')).toHaveLength(1);
  await row.click();
  await expect(row).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
  await expect.poll(() => storedReads.length).toBe(1);
  expect(requests.filter((r) => r.output === 'performance')).toHaveLength(0);
  await expect(page.getByLabel('Signal heatmap')).toBeHidden();
  await row.click();
  await expect(row).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  await row.focus();
  for (const [key, expanded] of [['Enter', 'true'], [' ', 'false']]) {
    await page.keyboard.press(key === ' ' ? 'Space' : key);
    await expect(row).toHaveAttribute('aria-expanded', expanded);
  }
  await expect(page.getByLabel('Signal heatmap')).toBeVisible();
  expect(storedReads).toHaveLength(1);
  expect(requests.filter((r) => r.output === 'signals')).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('a row of an evicted run falls back to simulating the candidate', async ({ page }) => {
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  const requests: { output: string }[] = [];
  const { storedReads } = await mockRun(
    page,
    (route) => {
      if (route.request().url().endsWith('/api/simulate')) requests.push(route.request().postDataJSON());
      const response = structuredClone(fixture) as SimulationResponse;
      response.alphas[0].signals!.logDistribution = {
        ...response.alphas[0].signals!.distribution!,
        scale: 'linear',
      };
      return route.fulfill({ json: response });
    },
    { evicted: true },
  );
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await page.getByRole('button', { name: `View alpha_${fixture.alphas[0].id}`, exact: true }).click();
  await expect(page.getByRole('table', { name: 'Yearly performance' })).toBeVisible();
  await expect.poll(() => storedReads.length).toBe(1);
  await expect.poll(() => requests.filter((r) => r.output === 'performance').length).toBe(1);
});
