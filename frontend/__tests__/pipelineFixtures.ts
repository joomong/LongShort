import { readFileSync } from 'node:fs';

function readJson<T = unknown>(relativePath: string): T {
  const url = new URL(relativePath, import.meta.url);
  return JSON.parse(readFileSync(url, 'utf-8')) as T;
}

function readText(relativePath: string): string {
  const url = new URL(relativePath, import.meta.url);
  return readFileSync(url, 'utf-8');
}

export const singleAlphaFixture = readJson('../../tests/fixtures/pipeline/single_alpha_full_payload.json');
export const multiAlphaFixture = readJson('../../tests/fixtures/pipeline/multi_alpha_full_payload.json');
export const gaStreamEventsFixture = readText('../../tests/fixtures/pipeline/ga_stream_events.ndjson');
