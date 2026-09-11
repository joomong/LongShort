// Capture small, real C++ API responses. Run against the team's synthetic engine fixture.
// node scripts/capture_engine_fixtures.mjs http://127.0.0.1:PORT
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const base = process.argv[2] ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000';
const target = fileURLToPath(new URL('../__tests__/fixtures/', import.meta.url));
const configResponse = await fetch(`${base}/api/config`);
if (!configResponse.ok) throw new Error(`Config failed: ${configResponse.status}`);
const config = await configResponse.json();
if (config.N < 3 || config.T < 4) throw new Error('Use a synthetic panel with at least 3 symbols and 4 bars');
const last = Date.parse(config.simEnd + 'T00:00:00Z');
const first = Math.max(Date.parse(config.simStart + 'T00:00:00Z'), last - 119 * 86400000);
const simulationStart = new Date(first).toISOString().slice(0, 10);
const midpoint = new Date(first + Math.max(1, Math.floor((last - first) / 86400000 / 2)) * 86400000).toISOString().slice(0, 10);
const settings = { simStart: simulationStart, isStart: simulationStart, osStart: midpoint, mode: 1, threads: 1, collectSignals: true, collectSignalsTopK: 5, signalPlotMaxCells: 60 };
const singleXml = '<data expr="close" delay="0"/><zscore/>';
const multiXml = '<data expr="close" delay="0"/><tsmean days="2|3"/><zscore/>';
async function post(path, body) {
  const response = await fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${path}: ${response.status} ${text}`);
  return text;
}
await mkdir(target, { recursive: true });
for (const [name, xml] of [['cpp_single', singleXml], ['cpp_multi', multiXml]]) {
  const response = JSON.parse(await post('/api/simulate', { ...settings, xml }));
  if (!response.signalPlot || !response.batches.length) throw new Error(`${name}: signal payload missing`);
  await writeFile(`${target}/${name}.json`, JSON.stringify(response, null, 2) + '\n');
}
const events = await post('/api/ga/stream', { ...settings, xml: multiXml, popSize: 4, generations: 1, threads: 1 });
const parsed = events.trim().split('\n').map(line => JSON.parse(line));
if (!parsed.some(event => event.type === 'complete' && event.signalPlot)) throw new Error(`GA completion signal payload missing: ${events}`);
await writeFile(`${target}/cpp_ga_events.ndjson`, events.trim() + '\n');
await writeFile(`${target}/capture.json`, JSON.stringify({ config, settings, singleXml, multiXml }, null, 2) + '\n');
console.log(`Captured actual C++ responses in ${target}`);
