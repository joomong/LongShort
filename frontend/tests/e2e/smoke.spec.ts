import { test, expect } from '@playwright/test';

test.describe('Smoke tests', () => {
  test('page loads with correct title', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/LongShort/);
  });

  test('control buttons are present', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByLabel('Run Backtest (Shift+Enter)')).toBeVisible();
    await expect(page.getByLabel('Save Alpha')).toBeVisible();
    await expect(page.getByLabel('Load Alpha')).toBeVisible();
  });

  test('main layout panels are visible', async ({ page }) => {
    await page.goto('/');
    // Editor panel (left) and visualization panel (right)
    const panels = page.locator('[class*="flex"]').first();
    await expect(panels).toBeVisible();
  });
});
