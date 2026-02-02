import { chromium } from 'playwright';

const url = process.env.URL || 'http://127.0.0.1:3000/';

const run = async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });

  page.on('request', (req) => {
    if (req.isNavigationRequest()) console.log('NAV_REQ', req.url());
  });
  page.on('requestfailed', (req) => {
    console.log('REQ_FAILED', req.url(), req.failure());
  });
  page.on('response', (res) => {
    if (res.request().isNavigationRequest()) console.log('NAV_RES', res.status(), res.url());
  });
  page.on('pageerror', (err) => console.log('PAGE_ERROR', String(err)));
  page.on('console', (msg) => console.log('CONSOLE', msg.type(), msg.text()));

  try {
    await page.goto(url, { waitUntil: 'commit', timeout: 15000 });
    console.log('GOTO_COMMIT_OK', page.url());
  } catch (e) {
    console.log('GOTO_ERROR', e?.name, e?.message);
  }

  // try to wait for domcontentloaded separately
  try {
    await page.waitForLoadState('domcontentloaded', { timeout: 15000 });
    console.log('DCL_OK');
  } catch (e) {
    console.log('DCL_ERROR', e?.name, e?.message);
  }

  const html = await page.content().catch((e) => `CONTENT_ERROR: ${e}`);
  console.log('HTML_LEN', typeof html === 'string' ? html.length : -1);

  await page.screenshot({ path: '/tmp/pw_debug.png', fullPage: true }).catch((e) => console.log('SCREENSHOT_ERROR', e?.message));
  console.log('WROTE /tmp/pw_debug.png');

  await browser.close();
};

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
