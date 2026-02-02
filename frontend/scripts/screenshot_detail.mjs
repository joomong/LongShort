import { chromium } from 'playwright';

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

const run = async () => {
  // Pre-fetch a real response from the backend, then stub the frontend request.
  const sample = await fetchSampleBacktest();

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--proxy-bypass-list=*'],
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  page.on('console', (msg) => {
    if (msg.type() === 'error') console.error('BROWSER_CONSOLE_ERROR:', msg.text());
  });

  await page.route('**/api/backtest/run', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(sample),
    });
  });

  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });

  // Click "Run Backtest" (play button)
  const runBtn = page.getByTitle('Run Backtest');
  await runBtn.waitFor({ state: 'visible', timeout: 60000 });

  const reqP = page.waitForRequest((req) => req.url().includes('/api/backtest/run'), { timeout: 60000 }).catch(() => null);
  await runBtn.click({ force: true });
  const req = await reqP;
  if (!req) {
    // If the app didn't fire the request, still take a screenshot for debugging.
    await page.screenshot({ path: out, fullPage: true });
    await browser.close();
    console.log(out);
    return;
  }

  // Wait for results table rows to appear
  await page.waitForSelector('text=Backtest Results', { timeout: 120000 });
  await page.waitForSelector('tbody tr', { timeout: 120000 });

  // Click first row to enter detail
  await page.locator('tbody tr').first().click();

  // Wait a beat for plot swap
  await sleep(1000);

  await page.screenshot({ path: out, fullPage: true });
  await browser.close();

  console.log(out);
};

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
