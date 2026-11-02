import { readFileSync } from 'node:fs';
import { adaptSimulateResponse, adaptGACompleteToResponse } from '@/lib/api';
import type { StarBatchResponse, StarGAEvent } from '@/types';

function readText(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf-8');
}
function readJson<T>(relativePath: string): T {
  return JSON.parse(readText(relativePath)) as T;
}

// Captured from a synthetic C++ integration server (fixtures/capture.json holds the
// /api/config it answered). No Python-engine dependency.
export const singleStarFixture = readJson<StarBatchResponse>('./fixtures/cpp_single.json');
export const multiStarFixture = readJson<StarBatchResponse>('./fixtures/cpp_multi.json');
export const singleAlphaFixture = adaptSimulateResponse(singleStarFixture);
export const multiAlphaFixture = adaptSimulateResponse(multiStarFixture);
export const gaStreamEventsFixture = readText('./fixtures/cpp_ga_events.ndjson');
const complete = gaStreamEventsFixture.trim().split('\n').map(line => JSON.parse(line) as StarGAEvent).find(event => event.type === 'complete');
if (!complete || complete.type !== 'complete') throw new Error('C++ GA fixture has no completion event');
export const gaCompleteFixture = complete;
export const gaAlphaFixture = adaptGACompleteToResponse(gaCompleteFixture);
