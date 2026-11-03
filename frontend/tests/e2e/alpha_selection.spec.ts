import { mockRun } from '../helpers/mockRun';
import { test, expect } from '@playwright/test';
import fixture from '../unit/fixtures/cpp_multi.json';
import captured from '../unit/fixtures/capture.json';

test('row and batch circles only select; saving stores the editor XML and run rows', async ({
  page,
}) => {
  const saved: Record<string, unknown>[] = [];
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
  await mockRun(page, (route) => route.fulfill({ json: fixture }));
  await page.route('**/api/alphas', (route) => {
    if (route.request().method() === 'POST') saved.push(route.request().postDataJSON());
    return route.fulfill({ json: { saved: true } });
  });
  await page.goto('/');
  await page.getByLabel('Run Backtest (Shift+Enter)').click();
  const batch = page.getByRole('button', { name: 'View combined alpha', exact: true });
  await batch.click();
  await expect(batch).toHaveAttribute('aria-expanded', 'true');
  await expect(batch.locator('td').first()).toHaveText('batch');
  await expect(page.getByLabel('Save alpha', { exact: true })).toBeEnabled();
  await batch.getByRole('button', { name: 'Select all alphas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Deselect alpha', exact: true })).toHaveCount(2);
  await expect(page.getByRole('heading', { name: 'Save Alpha', exact: true })).toHaveCount(0);
  await batch.getByRole('button', { name: 'Deselect all alphas', exact: true }).click();

  await page.getByPlaceholder('alpha name', { exact: true }).fill('plain');
  await page.getByLabel('Save alpha', { exact: true }).click();
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ name: 'plain', level: 'xml' });
  expect(saved[0]).not.toHaveProperty('rows');

  await page.getByPlaceholder('alpha name', { exact: true }).fill('full');
  await page.getByLabel('Save alpha', { exact: true }).click();
  await page.getByLabel('XML + results', { exact: true }).check();
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => saved.length).toBe(2);
  expect(saved[1]).toMatchObject({ name: 'full', level: 'results', xml: saved[0].xml });
  expect((saved[1].rows as { xml: string }[]).map((row) => row.xml)).toEqual(
    fixture.alphas.map((alpha) => alpha.xml),
  );
});
