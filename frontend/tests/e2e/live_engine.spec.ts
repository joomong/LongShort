import { test, expect } from '@playwright/test';
import captured from '../../__tests__/fixtures/capture.json';
import type { StarBatchResponse, StarGACompleteEvent } from '../../types';

const engine = process.env.PLAYWRIGHT_ENGINE_URL;
test.skip(!engine, 'Set PLAYWRIGHT_ENGINE_URL to the synthetic C++ integration server');

test('live C++ D0/D1, GA signals and expanded XML save/load roundtrip', async ({ page, request }) => {
  test.setTimeout(90000);
  const prefix = `frontend_smoke_${Date.now()}`;
  const templateName = `${prefix}_template`;
  const names = [templateName, `${prefix}_alpha_0`, `${prefix}_alpha_1`, `${prefix}_detail`];
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const created = await request.post(`${engine}/api/alphas`, { data: { name: templateName, xml: captured.multiXml } });
  expect(created.ok()).toBe(true);
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const response = await route.fetch({ url: engine + path, timeout: 60000 });
    await route.fulfill({ response });
  });
  async function load(name: string) {
    await page.getByLabel('Load Alpha').click();
    const heading = page.getByRole('heading', { name, exact: true });
    await heading.locator('xpath=../..').getByRole('button', { name: 'Load', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Load Alpha', exact: true })).toHaveCount(0);
  }
  async function save(name: string) {
    await page.getByLabel('Pin Signal', { exact: true }).click();
    await page.getByLabel('Alpha Name', { exact: true }).fill(name);
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Save Alpha', exact: true })).toHaveCount(0);
  }
  try {
    await page.goto('/');
    await load(templateName);
    await page.getByLabel('IS start date').fill(captured.settings.isStart);
    await page.getByLabel('OS start date').fill(captured.settings.osStart);
    await page.getByLabel('Sim start date').fill(captured.settings.simStart);
    await page.getByLabel('Number of jobs').fill('2');
    let current!: StarBatchResponse;
    for (const [label, mode] of [['D1', 1], ['D0', 0]] as const) {
      await page.getByLabel('Bar interval').selectOption(label);
      const pending = page.waitForResponse(response => response.url().endsWith('/api/simulate') && response.request().method() === 'POST');
      await page.getByLabel('Run Backtest (Shift+Enter)').click();
      const response = await pending;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON()).toMatchObject({ mode, threads: 2 });
      current = await response.json() as StarBatchResponse;
      expect(current.mode).toBe(mode);
      await expect(page.getByText('Alpha Ranking (2)')).toBeVisible();
      await expect(page.getByLabel('Signal heatmap')).toBeVisible();
      await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
    }
    for (const index of [0, 1]) await page.getByRole('button', { name: `View alpha_${index}`, exact: true }).getByRole('button', { name: 'Select alpha', exact: true }).click();
    await save(prefix);
    let saved = await (await request.get(`${engine}/api/alphas`)).json() as { name: string; xml: string }[];
    for (const batch of current.batches) expect(saved.find(alpha => alpha.name === `${prefix}_alpha_${batch.index}`)?.xml).toBe(batch.xml);
    // Existing multi selections cannot override a detail save.
    await page.getByRole('button', { name: 'View alpha_0', exact: true }).getByRole('button', { name: 'Select alpha', exact: true }).click();
    await page.getByRole('button', { name: 'View alpha_1', exact: true }).click();
    await save(`${prefix}_detail`);
    saved = await (await request.get(`${engine}/api/alphas`)).json() as { name: string; xml: string }[];
    expect(saved.find(alpha => alpha.name === `${prefix}_detail`)?.xml).toBe(current.batches.find(batch => batch.index === 1)?.xml);
    await load(templateName);
    await expect(page.getByPlaceholder('alpha name')).toHaveValue(templateName);
    await page.getByLabel('Bar interval').selectOption('D1');
    await page.getByLabel('GA population size').fill('10');
    const pendingGA = page.waitForResponse(response => response.url().endsWith('/api/ga/stream') && response.request().method() === 'POST');
    await page.getByLabel('Run GA', { exact: true }).click();
    const gaResponse = await pendingGA;
    expect(gaResponse.status()).toBe(200);
    const complete = (await gaResponse.text()).trim().split('\n').map(line => JSON.parse(line)).find(event => event.type === 'complete') as StarGACompleteEvent;
    expect(complete.mode).toBe(1);
    expect(complete.signalPlot?.values.length).toBeGreaterThan(0);
    await expect(page.getByText('Alpha Ranking (1)')).toBeVisible();
    await expect(page.getByLabel('Signal heatmap')).toBeVisible();
    await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
    expect(errors).toEqual([]);
  } finally {
    for (const name of names) await request.delete(`${engine}/api/alphas/${name}`);
  }
});
