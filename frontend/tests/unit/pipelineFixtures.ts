import { readFileSync } from 'node:fs';
import type { SearchEvent, SimulationResponse } from '@/types';

function readText(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf-8');
}
function readJson<T>(relativePath: string): T {
  return JSON.parse(readText(relativePath)) as T;
}

export const singleResponseFixture = readJson<SimulationResponse>('./fixtures/cpp_single.json');
export const multiResponseFixture = readJson<SimulationResponse>('./fixtures/cpp_multi.json');
const gaStreamEventsFixture = readText('./fixtures/cpp_ga_events.ndjson');
export const searchEventsFixture = gaStreamEventsFixture
  .trim()
  .split('\n')
  .map((line) => JSON.parse(line) as SearchEvent);
