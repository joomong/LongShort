import { defineConfig } from '@playwright/test';

const port = Number(process.env.PLAYWRIGHT_PORT ?? 3000);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid PLAYWRIGHT_PORT');
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL,
    trace: 'on-first-retry',
    viewport: { width: 1680, height: 1000 },
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium', launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROME_PATH } },
    },
  ],
  webServer: {
    command: `${process.env.PLAYWRIGHT_USE_BUILD ? '' : 'NEXT_DIST_DIR=.next-playwright '}npm run ${process.env.PLAYWRIGHT_USE_BUILD ? 'start' : 'dev'} -- --hostname 127.0.0.1 --port ${port}`,
    url: baseURL,
    reuseExistingServer: !process.env.PLAYWRIGHT_PORT,
    timeout: 60000,
  },
});
