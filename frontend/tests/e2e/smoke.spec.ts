import { test, expect } from '@playwright/test';
import captured from '../unit/fixtures/capture.json';
test.beforeEach(async ({ page }) => {
  await page.route('**/api/config', (route) => route.fulfill({ json: captured.config }));
});

test.describe('Smoke tests', () => {
  test('page loads with correct title', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/LongShort/);
    await expect(page.getByText('Run an alpha to explore its results.')).toHaveCount(0);
  });

  test('control buttons are present', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByLabel('Run Backtest (Shift+Enter)')).toBeVisible();
    await expect(page.getByLabel('Save alpha')).toBeVisible();
    await expect(page.getByLabel('Load Alpha')).toBeVisible();
    const name = (await page.getByPlaceholder('alpha name').boundingBox())!;
    const save = (await page.getByLabel('Save alpha').boundingBox())!;
    const load = (await page.getByLabel('Load Alpha').boundingBox())!;
    const run = (await page.getByLabel('Run Backtest (Shift+Enter)').boundingBox())!;
    expect(save.x).toBeGreaterThan(name.x + name.width);
    expect(load.x).toBeGreaterThan(save.x);
    expect(Math.abs(save.y - name.y)).toBeLessThan(6);
    expect(run.x).toBeGreaterThan(page.viewportSize()!.width * 0.8);
    expect(run.y).toBeLessThan(40);
  });

  test('main layout panels are visible', async ({ page }) => {
    await page.goto('/');
    // Editor panel (left) and visualization panel (right)
    const panels = page.locator('[class*="flex"]').first();
    await expect(panels).toBeVisible();
  });
});
