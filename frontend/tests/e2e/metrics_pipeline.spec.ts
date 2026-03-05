import { test, expect } from '@playwright/test';

// StarBatchResponse mock — C++ STAR engine returns display-ready values:
// percentage fields as percentage points (e.g. 26.67 = 26.67%), counts as counts.
const batchResponse = {
  batches: [
    {
      index: 0,
      xml: "<data expr='close' delay='1'/>",
      summary: {
        is: { return: 947.455, sharpe: 5.06, mdd: -2.0, turnover: 26.67, pnl: 1.949, minBet: 3.5, maxWeight: 60, longRatio: 0.5, shortRatio: 0.5 },
        os: { return: -60.3, sharpe: -4.50, mdd: -1.0, pnl: -1.206, minBet: 0, maxWeight: 0, turnover: 0, longRatio: 0, shortRatio: 0 },
      },
      yearly: [
        { year: 2024, longSM: 0.6, shortSM: 0.4, pnl: 1.9, annRet: 9.4, sharpe: 5.06, maxDD: -2.0, dailyTvr: 0.26, margin: 0.01 },
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
    },
    {
      index: 1,
      xml: "<data expr='close' delay='1'/><tsmean days='20'/>",
      summary: {
        is: { return: 1247.95, sharpe: 6.00, mdd: -1.5, turnover: 30.0, pnl: 2.5, minBet: 4, maxWeight: 55, longRatio: 0.5, shortRatio: 0.5 },
        os: { return: -40.0, sharpe: -3.00, mdd: -0.8, pnl: -0.8, minBet: 0, maxWeight: 0, turnover: 0, longRatio: 0, shortRatio: 0 },
      },
      yearly: [
        { year: 2024, longSM: 0.62, shortSM: 0.38, pnl: 2.5, annRet: 12.4, sharpe: 6.0, maxDD: -1.5, dailyTvr: 0.30, margin: 0.02 },
      ],
      timeseries: {
        timestamps: ['2024-01-01', '2024-07-01', '2024-12-31'],
        cumPnl: [0.0, 0.012, 0.025],
        cumPnlGross: [0.0, 0.013, 0.027],
        turnover: [0.0, 0.006, 0.005],
        maxWeight: [0.0, 0.09, 0.08],
        nonzeroCount: [0, 3, 3],
        drawdown: [0.0, 0.0, 0.0],
      },
      isStartIndex: 0,
      osStartIndex: 2,
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

    // Alpha ranking table — 2 batches = 2 alphas (no Combined row)
    await expect(page.getByText('Alpha Ranking (2)')).toBeVisible();
    const alpha0Row = page.locator('tbody tr').filter({ hasText: 'alpha_0' });
    await expect(alpha0Row).toBeVisible();
    await expect(alpha0Row).toContainText('+947.5%');
    await expect(alpha0Row).toContainText('-60.3%');

    const alpha1Row = page.locator('tbody tr').filter({ hasText: 'alpha_1' });
    await expect(alpha1Row).toBeVisible();
    await expect(alpha1Row).toContainText('+1248.0%');

    // Enter detail and verify alpha-specific XML + yearly details switch correctly.
    await alpha1Row.click();
    await expect(page.locator('pre').filter({ hasText: "<data expr='close' delay='1'/><tsmean days='20'/>" })).toBeVisible();
    await expect(page.getByText('+12.4%')).toBeVisible();

    await alpha0Row.click();
    await expect(page.locator('pre').filter({ hasText: "<data expr='close' delay='1'/>" })).toBeVisible();
    await expect(page.getByText('+9.4%')).toBeVisible();
  });
});
