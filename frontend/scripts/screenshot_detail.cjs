const { chromium } = require('playwright');

const url = process.env.URL || 'http://127.0.0.1:3000';
const out = process.env.OUT || '/tmp/alpha_detail.png';
const apiBase = process.env.API || 'http://127.0.0.1:8000';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchSampleBacktest() {
  const body = {
    xml: '<data expr="close" delay="1"/>',
    settings: {
      isStart: '2023-01-01',
      isEnd: '2023-06-30',
      osStart: '2023-07-01',
      osEnd: '2023-12-31',
      barInterval: 'D1',
    },
  };
  const res = await fetch(`${apiBase}/api/backtest/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`sample backtest failed: ${res.status} ${await res.text()}`);
  return res.json();
}

(async () => {
  const sample = await fetchSampleBacktest();

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();

  await page.route('**/api/backtest/run', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sample) });
  });

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // Click Run
  const runBtn = page.getByTitle('Run Backtest');
  await runBtn.waitFor({ state: 'visible', timeout: 60000 });
  await runBtn.click({ force: true });

  // Wait for results label to disappear OR for any row to appear
  await page.waitForTimeout(1000);

  // If rows exist, click first row (detail)
  const rows = page.locator('tbody tr');
  const count = await rows.count();
  if (count > 0) {
    await rows.first().click({ force: true });
    await sleep(800);
  }

  await page.screenshot({ path: out, fullPage: true });
  await browser.close();

  console.log(out);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
