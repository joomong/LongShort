import { test, expect, request as playwrightRequest } from '@playwright/test';
import captured from '../unit/fixtures/capture.json';
import type { SearchEvent } from '../../src/types';

const engine = process.env.PLAYWRIGHT_ENGINE_URL;
// tsmean days="3" needs warmup after simStart; the shared capture has isStart == simStart.
const isStart = '2025-09-10';
test.skip(!engine, 'Set PLAYWRIGHT_ENGINE_URL to the synthetic C++ integration server');

// Saved names are removed with a fresh request context so cleanup also runs after a test timeout.
let names: string[] = [];
test.afterEach(async () => {
  if (!engine || names.length === 0) return;
  const cleanup = await playwrightRequest.newContext();
  try {
    for (const name of names)
      await cleanup
        .delete(`${engine}/api/alphas/${encodeURIComponent(name)}`)
        .catch(() => undefined);
  } finally {
    names = [];
    await cleanup.dispose();
  }
});

test('live C++ default GA explicit XML delay and expanded XML save/load roundtrip', async ({
  page,
  request,
}) => {
  test.setTimeout(90000);
  const prefix = `frontend_smoke_${Date.now()}`;
  const templateName = `${prefix}_template`;
  names = [templateName, prefix, `${prefix}_results`];
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const engineConfig = await (await request.get(`${engine}/api/config`)).json();
  const metadata = {
    simStart: captured.settings.simStart,
    simEnd: engineConfig.simEnd,
    isStart,
    osStart: captured.settings.osStart,
    universe: engineConfig.defaultUniverse ?? '',
    executionPrice: engineConfig.executionPrice ?? 'close',
    threads: 2,
  };
  const created = await request.post(`${engine}/api/alphas`, {
    data: { name: templateName, xml: captured.multiXml, level: 'xml', metadata },
  });
  expect(created.ok()).toBe(true);
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const response = await route.fetch({ url: engine + path, timeout: 60000 });
    await route.fulfill({ response });
  });
  async function load(name: string) {
    await page.getByLabel('Load Alpha').click();
    const heading = page.getByRole('heading', { name, exact: true });
    await heading
      .locator('xpath=../../..')
      .getByRole('button', { name: 'Load', exact: true })
      .click();
    await expect(page.getByRole('heading', { name: 'Load Alpha', exact: true })).toHaveCount(0);
  }
  async function save(name: string, level = 'XML only') {
    await page.getByPlaceholder('alpha name', { exact: true }).fill(name);
    await page.getByLabel('Save alpha', { exact: true }).click();
    await page.getByLabel(level, { exact: true }).check();
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Save Alpha', exact: true })).toHaveCount(0);
  }
  {
    await page.goto('/');
    await load(templateName);
    await page.getByLabel('IS start date').fill(isStart);
    await page.getByLabel('OS start date').fill(captured.settings.osStart);
    await page.getByLabel('Sim start date').fill(captured.settings.simStart);
    await page.getByLabel('Number of threads').fill('2');
    let current: { id: number; xml: string }[] = [];
    let sourceXml = '';
    {
      await expect(page.getByLabel('Bar interval')).toHaveCount(0);
      const pending = page.waitForResponse(
        (response) =>
          response.url().endsWith('/api/ga/stream') && response.request().method() === 'POST',
      );
      await page.getByLabel('Run Backtest (Shift+Enter)').click();
      const response = await pending;
      expect(response.status()).toBe(200);
      expect(response.request().postDataJSON()).toMatchObject({ threads: 2 });
      sourceXml = response.request().postDataJSON().xml;
      const events = (await response.text())
        .trim()
        .split('\n')
        .map((line) => JSON.parse(line)) as SearchEvent[];
      expect(events.find((event) => event.type === 'search_start')).not.toHaveProperty('mode');
      expect(events.at(-1)?.type).toBe('search_complete');
      current = events.flatMap((event) =>
        event.type === 'candidate' ? [{ id: event.candidate.id, xml: event.candidate.xml! }] : [],
      );
      await expect(page.getByRole('table', { name: 'Alpha results' })).toBeVisible();
      await expect(page.getByTestId('signal-analysis')).toHaveCount(0);
      await page.getByRole('button', { name: 'View combined alpha', exact: true }).click();
      await expect(
        page.getByRole('button', { name: 'View combined alpha', exact: true }),
      ).toHaveAttribute('aria-expanded', 'true');
      await expect(page.getByTestId('result-placeholder')).toHaveCount(0);
    }
    await page.getByRole('button', { name: 'View combined alpha', exact: true }).click();
    // Circles only select; saving stores the editor XML verbatim (and, for results, the run rows).
    for (const index of [0, 1])
      await page
        .getByRole('button', { name: `View alpha_${index}`, exact: true })
        .getByRole('button', { name: 'Select alpha', exact: true })
        .click();
    await save(prefix);
    await save(`${prefix}_results`, 'XML + results');
    const saved = (await (await request.get(`${engine}/api/alphas`)).json()) as {
      name: string;
      xml: string;
      level: string;
    }[];
    expect(saved.find((alpha) => alpha.name === prefix)).toMatchObject({
      xml: sourceXml,
      level: 'xml',
    });
    expect(saved.find((alpha) => alpha.name === `${prefix}_results`)).toMatchObject({
      xml: sourceXml,
      level: 'results',
    });
    const savedRows = (
      await (await request.get(`${engine}/api/alphas/${prefix}_results/results`)).json()
    ).rows as { id: number }[];
    expect(savedRows.map((row) => row.id).sort((a, b) => a - b)).toEqual(
      current.map((batch) => batch.id).sort((a, b) => a - b),
    );
    await load(templateName);
    await expect(page.getByPlaceholder('alpha name')).toHaveValue(templateName);
    await page.getByLabel('First search').fill('10');
    const pendingGA = page.waitForResponse(
      (response) =>
        response.url().endsWith('/api/ga/stream') && response.request().method() === 'POST',
    );
    await page.getByLabel('Run Backtest (Shift+Enter)', { exact: true }).click();
    const gaResponse = await pendingGA;
    expect(gaResponse.status()).toBe(200);
    const complete = (
      (await gaResponse.text())
        .trim()
        .split('\n')
        .map((line) => JSON.parse(line)) as SearchEvent[]
    ).find((event) => event.type === 'search_complete');
    expect(complete?.type).toBe('search_complete');
    if (complete?.type !== 'search_complete') throw new Error('GA did not complete');
    expect(complete.bestXml).toBeTruthy();
    await expect(page.getByRole('tab', { name: 'Round 1', exact: true })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Alpha results' })).toBeVisible();
    await expect(page.locator('.uplot:visible canvas')).toHaveCount(1);
    expect(errors).toEqual([]);
  }
});
