import { test, expect } from '@playwright/test';

// StarBatchResponse mock — C++ STAR engine returns fractions,
// frontend adapter (adaptBatchResponse) multiplies by 100 for display.
const batchResponse = {
  batches: [
    {
      index: 0,
      xml: "<data expr='close' delay='1'/>",
      summary: {
        is: { return: 9.47455, sharpe: 5.06, mdd: -0.02, turnover: 0.2667, pnl: 0.01949, minBet: 0.0035, maxWeight: 0.6, longRatio: 0.5, shortRatio: 0.5 },
        os: { return: -0.603, sharpe: -4.50, mdd: -0.01, pnl: -0.01206, minBet: 0, maxWeight: 0, turnover: 0, longRatio: 0, shortRatio: 0 },
      },
    },
    {
      index: 1,
      xml: "<data expr='close' delay='1'/><tsmean days='20'/>",
      summary: {
        is: { return: 12.4795, sharpe: 6.00, mdd: -0.015, turnover: 0.30, pnl: 0.025, minBet: 0.004, maxWeight: 0.55, longRatio: 0.5, shortRatio: 0.5 },
        os: { return: -0.40, sharpe: -3.00, mdd: -0.008, pnl: -0.008, minBet: 0, maxWeight: 0, turnover: 0, longRatio: 0, shortRatio: 0 },
      },
    },
  ],
  timeseries: {
    timestamps: ['2024-01-01', '2024-07-01', '2024-12-31'],
    cumPnl: [0.0, 0.01, 0.02],
    cumPnlGross: [0.0, 0.012, 0.022],
    turnover: [0.0, 0.005, 0.004],
    maxWeight: [0.0, 0.08, 0.07],
    nonzeroCount: [0, 3, 3],
    drawdown: [0.0, 0.0, 0.0],
  },
  isStartIndex: 0,
  osStartIndex: 2,
};

test.describe('Metrics pipeline (simulate -> UI render)', () => {
  test('shows deterministic metrics end-to-end after clicking Run', async ({ page }) => {
    await page.route('**/api/config', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          symbols: ['AAA', 'BBB', 'CCC'],
          simStart: '2024-01-01',
          simEnd: '2024-12-31',
          feeRate: 0.001,
          barsPerYear: 365,
        }),
      });
    });

    await page.route('**/api/simulate', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(batchResponse),
      });
    });

    await page.goto('/');

    await page.getByLabel('Run Backtest (Shift+Enter)').click();

    // Alpha ranking table — 2 batches = 2 alphas
    await expect(page.getByText('Alpha Ranking (2)')).toBeVisible();

    // alpha_0 metrics in ranking table (IS return, OS return)
    await expect(page.getByText('+947.5%')).toBeVisible();
    await expect(page.getByText('-60.3%')).toBeVisible();

    // alpha_1 visible with its IS return
    await expect(page.getByText('alpha_1')).toBeVisible();
    await expect(page.getByText('+1248.0%')).toBeVisible();
  });
});
