import { test, expect } from '@playwright/test';
import fixture from '../../__tests__/fixtures/cpp_multi.json';
import captured from '../../__tests__/fixtures/capture.json';

test('batch and row circles select the exact alphas saved from detail view', async ({ page }) => {
  const saved: { name: string; xml: string }[] = [];
  await page.route('**/api/config', route => route.fulfill({ json: captured.config }));
  await page.route('**/api/simulate', route => route.fulfill({ json: fixture }));
  await page.route('**/api/alphas', route => {
    if (route.request().method() === 'POST') saved.push(route.request().postDataJSON());
    return route.fulfill({ json: { saved: true } });
  });
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  await expect(page.getByRole('button', { name: 'View combined alpha', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByLabel('Pin Signal', { exact: true })).toBeDisabled();
  const batch = page.getByRole('button', { name: 'View combined alpha', exact: true });
  await expect(batch.locator('td').first()).toHaveText('batch');
  await batch.getByRole('button', { name: 'Select all alphas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'View combined alpha', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('button', { name: 'Deselect alpha', exact: true })).toHaveCount(2);
  await batch.getByRole('button', { name: 'Deselect all alphas', exact: true }).click();
  await expect(page.getByLabel('Pin Signal', { exact: true })).toBeDisabled();

  // Choose alpha 0 for saving, while inspecting alpha 1.
  await page.getByRole('button', { name: 'View alpha_0', exact: true }).getByRole('button', { name: 'Select alpha', exact: true }).click();
  await page.getByRole('button', { name: 'View alpha_1', exact: true }).click();
  await page.getByLabel('Pin Signal', { exact: true }).click();
  await expect(page.getByText('Saving 1 alpha(s):')).toBeVisible();
  await page.getByLabel('Alpha Name', { exact: true }).fill('chosen');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toEqual({ name: 'chosen', xml: fixture.batches[0].xml });
  await expect(page.getByRole('heading', { name: 'Save Alpha', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Pin Signal', { exact: true })).toBeDisabled();

  // Batch selection saves both expanded XMLs from the currently open detail.
  await batch.getByRole('button', { name: 'Select all alphas', exact: true }).click();
  await page.getByLabel('Pin Signal', { exact: true }).click();
  await page.getByLabel('Alpha Name', { exact: true }).fill('group');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => saved.length).toBe(3);
  expect(saved.slice(1)).toEqual(fixture.batches.map(alpha => ({ name: `group_alpha_${alpha.index}`, xml: alpha.xml })));
});
