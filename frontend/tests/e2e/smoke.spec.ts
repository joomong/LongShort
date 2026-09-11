import { test, expect } from '@playwright/test';
import captured from '../../__tests__/fixtures/capture.json';
test.beforeEach(async ({ page }) => { await page.route('**/api/config', route => route.fulfill({ json: captured.config })); });

test.describe('Smoke tests', () => {
  test('page loads with correct title', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/LongShort/);
  });

  test('control buttons are present', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByLabel('Run Backtest (Shift+Enter)')).toBeVisible();
    await expect(page.getByLabel('Pin Signal')).toBeVisible();
    await expect(page.getByLabel('Load Alpha')).toBeVisible();
  });

  test('main layout panels are visible', async ({ page }) => {
    await page.goto('/');
    // Editor panel (left) and visualization panel (right)
    const panels = page.locator('[class*="flex"]').first();
    await expect(panels).toBeVisible();
  });
});
